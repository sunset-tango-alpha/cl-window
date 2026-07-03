#!/usr/bin/env python3
"""
CL-Screener v2: two-tier alerts for post-launch concentrated liquidity
on young pump.fun tokens.

TIER 1 - EARLY (watchlist mode):
  Discovers DLMM/CLMM pools at creation via GeckoTerminal's new_pools feed.
  Pools on pump.fun tokens with lag >= 6h go on a silent watchlist.
  Alert fires when a watched pool reaches $10K+ liquidity AND $10K+ 24h volume.
  Backtested (Jun 30 - Jul 1 cohort, 459 graduated tokens): 0 false alerts;
  would have alerted FABLE at ~$300-430K vs $950K for the ranked tier.

TIER 2 - CONFIRMED (ranked mode, unchanged from v1):
  Token's CL pool ranks in top-volume lists with $20K+ liq, 6h+ lag,
  $100K+ token-wide 24h volume. If a token already alerted EARLY, the
  CONFIRMED push is tagged as an escalation.

OUTCOME TRACKER (silent):
  Every alert logs MC at alert, then MC at +6h, +24h, +72h on later runs.
  Builds your hit-rate base per tier in state.json. No pushes.

State lives in state.json (committed back by the GitHub Actions workflow).
v1 state files are migrated automatically.
"""

import json
import os
import sys
import time
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ----------------------------- CONFIG ---------------------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE  = os.environ.get("STATE_FILE", "state.json")
WALLET_FILE = os.environ.get("WALLET_FILE", "wallet_state.json")

# shared
MAX_TOKEN_AGE_DAYS = 30
MIN_CL_LAG_HOURS   = 6
PUMP_ONLY          = True

# CONFIRMED tier (identical to v1)
CONF_MIN_CL_LIQ    = 20_000
CONF_MIN_TOKEN_VOL = 100_000
GT_DEXES           = ["meteora", "raydium-clmm"]
GT_PAGES           = 3

# EARLY tier (watchlist)
EARLY_MIN_LIQ      = 10_000
EARLY_MIN_VOL24    = 10_000
NEW_POOL_PAGES     = 2       # creation feed pages per run (20 pools/page)
WATCH_EXPIRE_H     = 72      # drop watched pools that never qualify

GT_SLEEP = 3.0               # GeckoTerminal free tier: 30 req/min

# Wallet tracking: buy/sell alerts via balance snapshot diffing per run
WATCH_WALLETS = [w.strip() for w in
                 os.environ.get("WATCH_WALLETS", "").split(",") if w.strip()]
WALLET_MIN_USD   = 100        # ignore balance changes worth less than this
WSOL             = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS   = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]

# LP concentration context on alerts
LP_CHECK      = True
DLMM_PROGRAM  = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
RPC_ENDPOINTS = ["https://api.mainnet-beta.solana.com",
                 "https://solana-rpc.publicnode.com"]

UA = {"User-Agent": "Mozilla/5.0 (cl-screener)", "Accept": "application/json"}

# ----------------------------- HELPERS --------------------------------------

def http_json(url, retries=3, backoff=8):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                time.sleep(backoff * (i + 1)); continue
            return None
        except Exception:
            if i < retries - 1:
                time.sleep(3); continue
            return None
    return None


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for ep in RPC_ENDPOINTS:
        for _ in range(2):
            try:
                req = urllib.request.Request(ep, data=body, headers={
                    "Content-Type": "application/json", **UA})
                with urllib.request.urlopen(req, timeout=40) as r:
                    d = json.loads(r.read())
                if "result" in d:
                    return d["result"]
            except Exception:
                pass
            time.sleep(5)
    return None


def b58encode(b: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = alphabet[r] + s
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + s


def ntfy(title, message, click=None, tags="chart_with_upwards_trend",
         priority="default"):
    if not NTFY_TOPIC:
        print(f"[dry-run push] {title} | {message.replace(chr(10), ' | ')}")
        return
    headers = {"Title": title, "Tags": tags, "Priority": priority, **UA}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                 data=message.encode(), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("ntfy push failed:", e)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lp_concentration(dlmm_pair):
    res = rpc("getProgramAccounts", [DLMM_PROGRAM, {
        "encoding": "base64",
        "dataSlice": {"offset": 40, "length": 32},
        "filters": [{"memcmp": {"offset": 8, "bytes": dlmm_pair}}]}])
    if not res:
        return None
    owners = {}
    for a in res:
        o = b58encode(base64.b64decode(a["account"]["data"][0]))
        owners[o] = owners.get(o, 0) + 1
    n = len(owners)
    kind = ("SINGLE-DESK" if n <= 3 else
            "concentrated" if n <= 15 else "crowd-farmed")
    return f"LP: {n} unique LPs / {len(res)} positions ({kind})"

# ----------------------------- STATE ----------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"alerts": {}, "watchlist": {}}
    with open(STATE_FILE) as f:
        s = json.load(f)
    if "alerts" not in s:  # migrate v1 (flat mint -> info) to v2
        s = {"alerts": {m: {"symbol": v.get("symbol"), "tier": "CONFIRMED",
                            "alert_time": v.get("first_seen"),
                            "mc_at_alert": v.get("mc_at_alert"),
                            "outcomes": {}} for m, v in s.items()},
             "watchlist": {}}
    s.setdefault("watchlist", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)

# ----------------------------- TIER 1: EARLY --------------------------------

def discover_new_cl_pools(state):
    """Creation-feed discovery -> add candidate CL pools to the watchlist."""
    added = 0
    for page in range(1, NEW_POOL_PAGES + 1):
        d = http_json("https://api.geckoterminal.com/api/v2/networks/solana/"
                      f"new_pools?page={page}")
        if not d:
            continue
        for p in d.get("data", []):
            dex = p["relationships"]["dex"]["data"]["id"]
            if dex not in GT_DEXES:
                continue
            base = p["relationships"]["base_token"]["data"]["id"]
            base = base.replace("solana_", "")
            if PUMP_ONLY and not base.endswith("pump"):
                continue
            pair = p["attributes"]["address"]
            if pair in state["watchlist"] or base in state["alerts"]:
                continue
            state["watchlist"][pair] = {"mint": base, "dex": dex,
                                        "added": now_iso(),
                                        "launch_ms": None}
            added += 1
        time.sleep(GT_SLEEP)
    return added


def evaluate_watchlist(state):
    """Check watched pools against EARLY qualification; push on cross."""
    wl = state["watchlist"]
    if not wl:
        return []
    # expire stale entries
    now = datetime.now(timezone.utc)
    for pair in list(wl):
        age_h = (now - datetime.fromisoformat(wl[pair]["added"])
                 ).total_seconds() / 3600
        if age_h > WATCH_EXPIRE_H:
            del wl[pair]

    # resolve token launch time once per mint (earliest pair on dexscreener)
    unresolved = sorted({w["mint"] for w in wl.values()
                         if w["launch_ms"] is None})
    for i in range(0, len(unresolved), 25):
        d = http_json("https://api.dexscreener.com/latest/dex/tokens/"
                      + ",".join(unresolved[i:i + 25]))
        if not d:
            continue
        firsts = {}
        for p in (d.get("pairs") or []):
            if p.get("pairCreatedAt"):
                m = p["baseToken"]["address"]
                firsts[m] = min(firsts.get(m, p["pairCreatedAt"]),
                                p["pairCreatedAt"])
        for w in wl.values():
            if w["mint"] in firsts:
                w["launch_ms"] = firsts[w["mint"]]
        time.sleep(1.5)

    alerts = []
    pairs = list(wl.keys())
    for i in range(0, len(pairs), 25):
        d = http_json("https://api.dexscreener.com/latest/dex/pairs/solana/"
                      + ",".join(pairs[i:i + 25]))
        if not d:
            continue
        for p in (d.get("pairs") or []):
            pair = p["pairAddress"]
            w = wl.get(pair)
            if not w or not p.get("pairCreatedAt") or not w["launch_ms"]:
                continue
            labels = p.get("labels") or []
            is_cl = ((p["dexId"] == "meteora" and "DLMM" in labels) or
                     (p["dexId"] == "raydium" and "CLMM" in labels))
            if not is_cl:                      # e.g. DYN2/DAMM pool: not ours
                del wl[pair]
                continue
            lag_h = (p["pairCreatedAt"] - w["launch_ms"]) / 3_600_000
            if lag_h < MIN_CL_LAG_HOURS:       # launch-hour pool: never ours
                del wl[pair]
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            vol = (p.get("volume") or {}).get("h24") or 0
            mint = w["mint"]
            if liq >= EARLY_MIN_LIQ and vol >= EARLY_MIN_VOL24 \
                    and mint not in state["alerts"]:
                alerts.append({"mint": mint,
                               "symbol": p["baseToken"]["symbol"],
                               "pair": pair,
                               "kind": "DLMM" if p["dexId"] == "meteora"
                                       else "CLMM",
                               "lag_h": lag_h, "liq": liq, "vol": vol,
                               "mc": p.get("marketCap") or p.get("fdv") or 0})
                del wl[pair]
        time.sleep(1.5)
    return alerts

# ----------------------------- TIER 2: CONFIRMED ----------------------------

def ranked_candidates():
    addrs = set()
    for dex in GT_DEXES:
        for page in range(1, GT_PAGES + 1):
            d = http_json("https://api.geckoterminal.com/api/v2/networks/"
                          f"solana/dexes/{dex}/pools?page={page}"
                          "&sort=h24_volume_usd_desc")
            if not d:
                continue
            for p in d.get("data", []):
                base = p["relationships"]["base_token"]["data"]["id"]
                base = base.replace("solana_", "")
                if PUMP_ONLY and not base.endswith("pump"):
                    continue
                addrs.add(base)
            time.sleep(GT_SLEEP)
    return list(addrs)


def confirmed_matches(addrs):
    now_ms = time.time() * 1000
    out = []
    for i in range(0, len(addrs), 25):
        d = http_json("https://api.dexscreener.com/latest/dex/tokens/"
                      + ",".join(addrs[i:i + 25]))
        if not d:
            continue
        by_tok = {}
        for p in (d.get("pairs") or []):
            by_tok.setdefault(p["baseToken"]["address"], []).append(p)
        for tok, pairs in by_tok.items():
            pairs = [p for p in pairs if p.get("pairCreatedAt")]
            if not pairs:
                continue
            first = min(p["pairCreatedAt"] for p in pairs)
            if (now_ms - first) / 86_400_000 > MAX_TOKEN_AGE_DAYS:
                continue
            vol24 = sum((p.get("volume") or {}).get("h24") or 0
                        for p in pairs)
            if vol24 < CONF_MIN_TOKEN_VOL:
                continue
            cl = []
            for p in pairs:
                labels = p.get("labels") or []
                is_dlmm = p["dexId"] == "meteora" and "DLMM" in labels
                is_clmm = p["dexId"] == "raydium" and "CLMM" in labels
                if not (is_dlmm or is_clmm):
                    continue
                lag_h = (p["pairCreatedAt"] - first) / 3_600_000
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if lag_h >= MIN_CL_LAG_HOURS and liq >= CONF_MIN_CL_LIQ:
                    cl.append({"kind": "DLMM" if is_dlmm else "CLMM",
                               "pair": p["pairAddress"],
                               "lag_h": lag_h, "liq": liq})
            if not cl:
                continue
            main = max(pairs,
                       key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            out.append({"mint": tok, "symbol": main["baseToken"]["symbol"],
                        "cl": cl, "vol24": vol24,
                        "mc": main.get("marketCap") or main.get("fdv") or 0})
        time.sleep(1.5)
    return out

# ----------------------------- OUTCOME TRACKER ------------------------------

def track_outcomes(state):
    """Silently log MC at +6h/+24h/+72h for past alerts."""
    due = []
    now = datetime.now(timezone.utc)
    for mint, a in state["alerts"].items():
        if not a.get("alert_time"):
            continue
        try:
            t0 = datetime.fromisoformat(a["alert_time"])
        except ValueError:
            continue
        h = (now - t0).total_seconds() / 3600
        a.setdefault("outcomes", {})
        for mark, label in [(6, "mc_6h"), (24, "mc_24h"), (72, "mc_72h")]:
            if h >= mark and label not in a["outcomes"]:
                due.append(mint)
                break
    due = sorted(set(due))
    for i in range(0, len(due), 25):
        d = http_json("https://api.dexscreener.com/latest/dex/tokens/"
                      + ",".join(due[i:i + 25]))
        if not d:
            continue
        mcs = {}
        for p in (d.get("pairs") or []):
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            mc = p.get("marketCap") or p.get("fdv") or 0
            if m not in mcs or liq > mcs[m][1]:
                mcs[m] = (mc, liq)
        for m in due[i:i + 25]:
            a = state["alerts"].get(m)
            if not a:
                continue
            t0 = datetime.fromisoformat(a["alert_time"])
            h = (now - t0).total_seconds() / 3600
            mc = mcs.get(m, (0, 0))[0]
            for mark, label in [(6, "mc_6h"), (24, "mc_24h"), (72, "mc_72h")]:
                if h >= mark and label not in a["outcomes"]:
                    a["outcomes"][label] = mc
        time.sleep(1.5)
    return len(due)


# ----------------------------- WALLET TRACKING -------------------------------

def fetch_wallet_balances(wallet):
    """All SPL token balances for a wallet (classic + Token-2022)."""
    balances = {}
    ok = False
    for prog in TOKEN_PROGRAMS:
        res = rpc("getTokenAccountsByOwner",
                  [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        if res is None:
            continue
        ok = True
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amt = float(info["tokenAmount"]["uiAmount"] or 0)
            if amt > 0 and mint != WSOL:
                balances[mint] = balances.get(mint, 0) + amt
    return balances if ok else None


def load_wallet_state():
    if os.path.exists(WALLET_FILE):
        with open(WALLET_FILE) as f:
            return json.load(f)
    return {}


def save_wallet_state(ws):
    with open(WALLET_FILE, "w") as f:
        json.dump(ws, f, indent=1)


def wallet_alerts(ws):
    """Diff balances vs last run; push buy/sell alerts with price context."""
    pushed = 0
    for wallet in WATCH_WALLETS:
        current = fetch_wallet_balances(wallet)
        if current is None:                 # RPC down: keep old snapshot
            print(f"  wallet {wallet[:8]}: RPC unavailable, skipping")
            continue
        prev_entry = ws.get(wallet)
        if prev_entry is None:              # first run: seed silently
            ws[wallet] = {"balances": current, "seeded": now_iso()}
            print(f"  wallet {wallet[:8]}: snapshot seeded, "
                  f"{len(current)} tokens (no alerts on first run)")
            continue
        prev = prev_entry["balances"]
        changed = {}
        for mint in set(prev) | set(current):
            delta = current.get(mint, 0) - prev.get(mint, 0)
            if abs(delta) > 1e-9:
                changed[mint] = delta
        if not changed:
            ws[wallet]["balances"] = current
            continue

        # price + symbol lookup for changed mints
        meta = {}
        mints = sorted(changed)
        for i in range(0, len(mints), 25):
            d = http_json("https://api.dexscreener.com/latest/dex/tokens/"
                          + ",".join(mints[i:i + 25]))
            if not d:
                continue
            for p in (d.get("pairs") or []):
                m = p["baseToken"]["address"]
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m in changed and (m not in meta or liq > meta[m]["liq"]):
                    meta[m] = {"symbol": p["baseToken"]["symbol"],
                               "price": float(p.get("priceUsd") or 0),
                               "liq": liq}
            time.sleep(1.5)

        for mint, delta in changed.items():
            m = meta.get(mint, {"symbol": mint[:6] + "...", "price": 0})
            usd = abs(delta) * m["price"]
            if m["price"] == 0:
                continue                    # unpriced token: noise, skip
            if usd < WALLET_MIN_USD:
                continue
            before, after = prev.get(mint, 0), current.get(mint, 0)
            if delta > 0:
                pct = ("NEW position" if before == 0 else
                       f"+{delta / before * 100:.0f}% to position")
                title = f"WALLET BUY: {m['symbol']}"
                tags, prio = "green_circle", "default"
            else:
                pct = ("CLOSED position (-100%)" if after == 0 else
                       f"-{-delta / before * 100:.0f}% of position")
                title = f"WALLET SELL: {m['symbol']}"
                tags, prio = "red_circle", "high"
            msg = (f"wallet {wallet[:4]}...{wallet[-4:]}\n"
                   f"{'+' if delta > 0 else ''}{delta:,.0f} {m['symbol']} "
                   f"(~${usd:,.0f}) | {pct}\n"
                   f"balance {before:,.0f} -> {after:,.0f}\n{mint}")
            ntfy(title, msg,
                 click=f"https://solscan.io/account/{wallet}",
                 tags=tags, priority=prio)
            pushed += 1
        ws[wallet]["balances"] = current
    return pushed

# ----------------------------- MAIN -----------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "--wallets-only":
        ws = load_wallet_state()
        print(f"[{now_iso()}] wallet run | {len(ws)} wallets tracked")
        pushed = wallet_alerts(ws)
        save_wallet_state(ws)
        print(f"  wallet alerts: {pushed}")
        return

    state = load_state()
    print(f"[{now_iso()}] run start | {len(state['alerts'])} alerted, "
          f"{len(state['watchlist'])} watched")

    # Tier 1: EARLY
    added = discover_new_cl_pools(state)
    early = evaluate_watchlist(state)
    for m in early:
        lp = lp_concentration(m["pair"]) if (LP_CHECK and
                                             m["kind"] == "DLMM") else None
        msg = (f"age of pool: +{m['lag_h']:.0f}h after launch\n"
               f"MC ${m['mc']:,.0f} | pool liq ${m['liq']:,.0f} | "
               f"pool vol24 ${m['vol']:,.0f}"
               + (f"\n{lp}" if lp else "") + f"\n{m['mint']}")
        ntfy(f"EARLY: {m['symbol']} ({m['kind']} +{m['lag_h']:.0f}h)", msg,
             click=f"https://dexscreener.com/solana/{m['mint']}",
             tags="hourglass_flowing_sand", priority="default")
        state["alerts"][m["mint"]] = {"symbol": m["symbol"], "tier": "EARLY",
                                      "alert_time": now_iso(),
                                      "mc_at_alert": m["mc"], "outcomes": {}}

    # Tier 2: CONFIRMED
    matches = confirmed_matches(ranked_candidates())
    for m in matches:
        prev = state["alerts"].get(m["mint"])
        if prev and prev["tier"] in ("CONFIRMED", "ESCALATED"):
            continue
        escalated = bool(prev)  # was EARLY
        lp = None
        if LP_CHECK:
            dlmm = next((c for c in m["cl"] if c["kind"] == "DLMM"), None)
            if dlmm:
                lp = lp_concentration(dlmm["pair"])
        cl_desc = "; ".join(f"{c['kind']} +{c['lag_h']:.0f}h ${c['liq']:,.0f}"
                            for c in m["cl"])
        head = ("ESCALATION (was EARLY at "
                f"${prev['mc_at_alert']:,.0f} MC)\n") if escalated else ""
        msg = (head + f"MC ${m['mc']:,.0f} | token vol24 ${m['vol24']:,.0f}\n"
               f"{cl_desc}" + (f"\n{lp}" if lp else "") + f"\n{m['mint']}")
        title = (f"CONFIRMED{'++' if escalated else ''}: {m['symbol']}")
        ntfy(title, msg,
             click=f"https://dexscreener.com/solana/{m['mint']}",
             tags="rotating_light", priority="high")
        if escalated:
            prev.update({"tier": "ESCALATED",
                         "escalated_time": now_iso(),
                         "mc_at_escalation": m["mc"]})
        else:
            state["alerts"][m["mint"]] = {"symbol": m["symbol"],
                                          "tier": "CONFIRMED",
                                          "alert_time": now_iso(),
                                          "mc_at_alert": m["mc"],
                                          "outcomes": {}}

    # Outcome tracker
    tracked = track_outcomes(state)

    save_state(state)
    print(f"  watchlist +{added} new, {len(state['watchlist'])} active | "
          f"EARLY alerts: {len(early)} | CONFIRMED checked: {len(matches)} | "
          f"outcomes updated: {tracked}")


if __name__ == "__main__":
    sys.exit(main())
