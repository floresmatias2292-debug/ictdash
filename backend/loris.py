"""Loris Tools (loris.tools): live funding rates of every venue listing the market (Omni/Variational, Binance,
Hyperliquid HIP-3 ...), read from GET /funding.

Response (docs): {"funding_rates": {exchange: {symbol: bps}}, "funding_rates_raw": {...}, "funding_intervals":
{exchange: {symbol: hours}}, "oi_rankings": {...}, "timestamp": ...}. funding_rates are 8 h-normalized basis
points (÷ 10,000 = decimal rate per 8 h). The free plan returns BTC and ETH only; stock perps (NVDA, AMD ...)
need a paid plan.

Key in .env (project root):  LORIS_API_KEY=...      optional: LORIS_API_URL, LORIS_POLL_SEC (default 60)
Check from the project folder:  .venv/bin/python -m backend.loris
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time

import httpx

from .config import DB_PATH, ORACLE_ALIASES, ROOT, SYMBOLS

log = logging.getLogger("loris")

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SAMPLE = ROOT / "data" / "loris_last.json"      # last raw response, for checking symbol / exchange names
# Venue names as Loris spells them -> the venue this dashboard trades or charts.
HOME_VENUES = {"variational": "Omni", "omni": "Omni", "binance": "Binance", "hyperliquid": "Hyperliquid",
               "tradexyz": "Hyperliquid xyz", "xyz": "Hyperliquid xyz"}


def base_url() -> str:
    return os.environ.get("LORIS_API_URL", "https://api.loris.tools").rstrip("/")


def api_key() -> str:
    return os.environ.get("LORIS_API_KEY", "").strip()


def poll_sec() -> int:
    return max(30, int(os.environ.get("LORIS_POLL_SEC", "60")))


def _norm(s: str) -> str:
    """'xyz:NVDA', 'NVDA-PERP', 'NVDAUSDT', 'nvda_usd' -> 'NVDA'."""
    s = str(s).upper().split(":")[-1]
    s = re.sub(r"[-_/ ]?(PERP|SWAP|USDT|USDC|USD)$", "", s)
    s = re.sub(r"[-_/ ]?(PERP|SWAP)$", "", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _aliases(symbol: str) -> set[str]:
    return {_norm(a) for a in ORACLE_ALIASES.get(symbol, [symbol])} | {_norm(symbol)}


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


async def fetch(client: httpx.AsyncClient) -> dict:
    headers = {"X-Api-Key": api_key()} if api_key() else {}
    r = await client.get(f"{base_url()}/funding", headers=headers, timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError(f"Loris {r.status_code}: API key missing or not accepted (LORIS_API_KEY in .env)")
    if r.status_code == 429:
        raise RuntimeError("Loris 429: rate limit, slowing down (LORIS_POLL_SEC)")
    r.raise_for_status()
    j = r.json()
    try:
        SAMPLE.parent.mkdir(parents=True, exist_ok=True)
        SAMPLE.write_text(json.dumps(j)[:2_000_000])
    except OSError:
        pass
    return j


def parse(j: dict) -> dict:
    """{symbol: {"venues": [{exchange, label, bps_8h, apr, interval_h, raw}], "ts", ...}} for every dashboard symbol."""
    rates = j.get("funding_rates") or {}
    raw = j.get("funding_rates_raw") or {}
    ivals = j.get("funding_intervals") or {}
    ts = _num(j.get("timestamp")) or time.time()
    if ts > 1e12:
        ts /= 1000
    out = {}
    for sym in SYMBOLS:
        al = _aliases(sym)
        venues = []
        for ex, per in rates.items():
            if not isinstance(per, dict):
                continue
            for name, v in per.items():
                bps = _num(v)
                if bps is None or _norm(name) not in al:
                    continue
                venues.append({"exchange": ex, "symbol": name, "label": HOME_VENUES.get(ex.lower()),
                               "bps_8h": bps, "apr": bps / 1e4 * 3 * 365 * 100,       # % per year
                               "interval_h": _num((ivals.get(ex) or {}).get(name)),
                               "raw": _num((raw.get(ex) or {}).get(name))})
        venues.sort(key=lambda v: v["bps_8h"])
        if not venues:
            out[sym] = {"venues": [], "ts": ts}
            continue
        lo, hi = venues[0], venues[-1]
        rs = sorted(v["bps_8h"] for v in venues)
        med = rs[len(rs) // 2] if len(rs) % 2 else (rs[len(rs) // 2 - 1] + rs[len(rs) // 2]) / 2
        out[sym] = {"venues": venues, "ts": ts, "median_bps": med,
                    "best_long": lo["exchange"], "best_short": hi["exchange"],          # longs receive where funding is lowest
                    "spread_bps": hi["bps_8h"] - lo["bps_8h"], "spread_apr": (hi["bps_8h"] - lo["bps_8h"]) / 1e4 * 3 * 365 * 100,
                    "omni": next((v for v in venues if v["label"] == "Omni"), None)}
    return out


# ── own history (Loris' historical endpoint is a paid feature; we keep what we read) ──
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS loris_funding (ts INTEGER, symbol TEXT, exchange TEXT, bps REAL, "
                "PRIMARY KEY (ts, symbol, exchange))")
    return con


def save(parsed: dict) -> None:
    rows = [(int(d["ts"]) // 60 * 60, sym, v["exchange"], v["bps_8h"]) for sym, d in parsed.items() for v in d["venues"]]
    if rows:
        with _db() as con:
            con.executemany("INSERT OR REPLACE INTO loris_funding VALUES (?,?,?,?)", rows)


def history(symbol: str, hours: int = 72) -> dict:
    """{exchange: [[ts, bps], ...]} for the home venues + the cross-venue median, one point per 15 min."""
    since = int(time.time()) - hours * 3600
    with _db() as con:
        rows = con.execute("SELECT ts, exchange, bps FROM loris_funding WHERE symbol=? AND ts>=? ORDER BY ts",
                           (symbol, since)).fetchall()
    by_t: dict = {}
    for ts, ex, bps in rows:
        by_t.setdefault(ts // 900 * 900, {})[ex] = bps
    out: dict = {"median": []}
    for t, m in sorted(by_t.items()):
        vals = sorted(m.values())
        out["median"].append([t, vals[len(vals) // 2]])
        for ex, bps in m.items():
            if ex.lower() in HOME_VENUES:
                out.setdefault(ex, []).append([t, bps])
    return out


# ── check: .venv/bin/python -m backend.loris ────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def _check():
        print(f"url: {base_url()}/funding · key: {'yes' if api_key() else 'NO (free plan returns BTC/ETH only)'}")
        async with httpx.AsyncClient() as c:
            j = await fetch(c)
        rates = j.get("funding_rates") or {}
        syms = {_norm(s) for per in rates.values() if isinstance(per, dict) for s in per}
        print(f"{len(rates)} exchanges, {len(syms)} symbols. Raw response saved to {SAMPLE}")
        print("exchanges:", ", ".join(sorted(rates)))
        p = parse(j)
        for sym, d in p.items():
            if not d["venues"]:
                print(f"\n{sym}: not in the response" + ("" if syms - {"BTC", "ETH"} else " (only BTC/ETH: plan too low)"))
                continue
            print(f"\n{sym}: {len(d['venues'])} venues · median {d['median_bps']:+.2f} bps/8h · "
                  f"spread {d['spread_bps']:.2f} bps ({d['spread_apr']:.1f}% APR)")
            for v in d["venues"]:
                print(f"  {v['exchange']:<16} {v['symbol']:<14} {v['bps_8h']:+8.2f} bps/8h  {v['apr']:+7.1f}% APR"
                      f"{'  <- ' + v['label'] if v['label'] else ''}")
        return 0

    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_check()))
