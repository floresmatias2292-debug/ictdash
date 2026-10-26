"""Market data from free public APIs: Binance USD-M futures and Hyperliquid, cached in SQLite.

Candles are native per timeframe (5m / 15m / 1h / 4h), 24 hours a day, volume in base units.
Binance also gives taker-buy volume. Omni (Variational) is used only for the live mark price.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

import re
import statistics

from .config import (BINANCE_FAPI, COINGECKO_DERIVATIVES, COMPUTE_BARS, DB_PATH, HYPERLIQUID_INFO, OMNI_STATS_URL,
                     ORACLE_ALIASES, SYMBOLS, TF_MINUTES, TIMEFRAMES)

log = logging.getLogger("data")
ET = ZoneInfo("America/New_York")
COLS = ["ts", "open", "high", "low", "close", "volume", "taker_buy"]


# ── SQLite cache ────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS bars (symbol TEXT, tf TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                " close REAL, volume REAL, taker_buy REAL, PRIMARY KEY (symbol, tf, ts))")
    con.execute("CREATE TABLE IF NOT EXISTS omni_ticks (symbol TEXT, ts INTEGER, mark REAL, PRIMARY KEY (symbol, ts))")
    con.execute("CREATE TABLE IF NOT EXISTS oracle_ticks (symbol TEXT, ts INTEGER, index_med REAL, price_med REAL,"
                " n INTEGER, venue_index REAL, venue_mark REAL, PRIMARY KEY (symbol, ts))")
    return con


def _upsert(symbol: str, tf: str, rows: list[tuple]) -> None:
    if rows:
        with _db() as con:
            con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
                            [(symbol, tf, *r) for r in rows])


def load_bars(symbol: str, tf: str, limit: int | None = None) -> pd.DataFrame:
    """Newest `limit` bars (default COMPUTE_BARS[tf]) with ts (UTC epoch, bar open), OHLCV and closed flag."""
    n = limit or COMPUTE_BARS[tf]
    with _db() as con:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume, taker_buy FROM bars WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
            con, params=(symbol, tf, n))
    df = df.iloc[::-1].reset_index(drop=True)
    df["closed"] = (df["ts"] + TF_MINUTES[tf] * 60) <= time.time()
    return df


def last_ts(symbol: str, tf: str) -> int | None:
    with _db() as con:
        r = con.execute("SELECT MAX(ts) FROM bars WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
    return r[0]


def has_history(symbol: str) -> bool:
    with _db() as con:
        n = con.execute("SELECT COUNT(*) FROM bars WHERE symbol=? AND tf='5m'", (symbol,)).fetchone()[0]
    return n > 1000


# ── Binance USD-M futures ───────────────────────────────────────────────────
def _binance(client: httpx.Client, ticker: str, tf: str, start_ms: int | None, limit: int) -> list[tuple]:
    params = {"symbol": ticker, "interval": tf, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    r = client.get(f"{BINANCE_FAPI}/fapi/v1/klines", params=params, timeout=20)
    r.raise_for_status()
    # [openTime, o, h, l, c, volume, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
    return [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[9]))
            for k in r.json()]


def _binance_backfill(client: httpx.Client, sym: str, tf: str) -> int:
    """From the newest cached bar (or the listing) up to now, 1500 bars per request."""
    ticker = SYMBOLS[sym]["ticker"]
    lt = last_ts(sym, tf)
    start = (lt * 1000) if lt else 0
    total = 0
    while True:
        rows = _binance(client, ticker, tf, start, 1500)
        _upsert(sym, tf, rows)
        total += len(rows)
        if len(rows) < 1500:
            return total
        start = rows[-1][0] * 1000 + 1
        time.sleep(0.25)                                  # stay far below the 2400 weight/min limit


# ── Hyperliquid (HIP-3 dex markets like xyz:XYZ100) ─────────────────────────
def _hyperliquid(client: httpx.Client, coin: str, tf: str, start_ms: int, end_ms: int) -> list[tuple]:
    r = client.post(HYPERLIQUID_INFO, json={"type": "candleSnapshot",
                                           "req": {"coin": coin, "interval": tf, "startTime": start_ms, "endTime": end_ms}},
                    timeout=20)
    r.raise_for_status()
    return [(int(c["t"]) // 1000, float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"]), None)
            for c in r.json()]


def _hyperliquid_backfill(client: httpx.Client, sym: str, tf: str) -> int:
    """Hyperliquid serves the newest 5000 candles per interval; history before that is kept from earlier runs."""
    lt = last_ts(sym, tf)
    now = int(time.time() * 1000)
    rows = _hyperliquid(client, SYMBOLS[sym]["ticker"], tf, (lt * 1000) if lt else 0, now)
    _upsert(sym, tf, rows)
    return len(rows)


def refresh_history(symbol: str, full: bool = False) -> None:
    """Bring every timeframe up to date (backfills from the listing on the first run)."""
    src = SYMBOLS[symbol]["source"]
    with httpx.Client() as client:
        for tf in TIMEFRAMES:
            try:
                n = (_binance_backfill if src == "binance" else _hyperliquid_backfill)(client, symbol, tf)
                if full:
                    log.info("%s %s %s: %d bars", src, symbol, tf, n)
            except Exception as e:
                log.warning("%s %s %s failed: %s", src, symbol, tf, e)


async def fetch_latest(client: httpx.AsyncClient, symbol: str, tf: str) -> list[tuple]:
    """The last two candles (previous closed + forming) for live updates."""
    meta = SYMBOLS[symbol]
    if meta["source"] == "binance":
        r = await client.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": meta["ticker"], "interval": tf, "limit": 2},
                             timeout=10)
        r.raise_for_status()
        rows = [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[9])) for k in r.json()]
    else:
        now = int(time.time() * 1000)
        r = await client.post(HYPERLIQUID_INFO, json={"type": "candleSnapshot", "req": {
            "coin": meta["ticker"], "interval": tf, "startTime": now - 2 * TF_MINUTES[tf] * 60000, "endTime": now}}, timeout=10)
        r.raise_for_status()
        rows = [(int(c["t"]) // 1000, float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"]), None) for c in r.json()]
    _upsert(symbol, tf, rows)
    return rows


def bar_end(ts: int, tf: str) -> int:
    """Close time of a bar (24h markets: open + timeframe)."""
    return int(ts) + TF_MINUTES[tf] * 60


def session_open(now: float | None = None) -> bool:
    """NY cash session (09:30–16:00 ET, weekdays) — used only for labels; the perps trade around the clock."""
    t = datetime.fromtimestamp(now or time.time(), ET)
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 570 <= m < 960


# ── Omni (Variational): live mark only ──────────────────────────────────────
def save_omni_ticks(live: dict, ts: int) -> None:
    rows = [(sym, ts, q["mark"]) for sym, q in live.items() if q.get("mark")]
    with _db() as con:
        con.executemany("INSERT OR REPLACE INTO omni_ticks VALUES (?,?,?)", rows)


def omni_bucket(ts: int, tf: str) -> int:
    """Bar open (UTC epoch) on the 24h clock; the page shifts it to the chosen time zone."""
    sec = TF_MINUTES[tf] * 60
    return int(ts) - int(ts) % sec


def load_omni_bars(symbol: str, tf: str, days: int = 60) -> list[dict]:
    since = int(time.time()) - days * 86400
    with _db() as con:
        rows = con.execute("SELECT ts, mark FROM omni_ticks WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since)).fetchall()
    out: list[dict] = []
    for ts, px in rows:
        b = omni_bucket(ts, tf)
        if out and out[-1]["time"] == b:
            c = out[-1]
            c["high"], c["low"], c["close"] = max(c["high"], px), min(c["low"], px), px
        else:
            out.append({"time": b, "open": px, "high": px, "low": px, "close": px})
    return out


async def fetch_omni(client: httpx.AsyncClient) -> dict:
    r = await client.get(OMNI_STATS_URL, timeout=15)
    r.raise_for_status()
    listings = {x["ticker"]: x for x in r.json().get("listings", [])}
    out = {}
    for sym, meta in SYMBOLS.items():
        x = listings.get(meta["omni"])
        if not x:
            continue
        f = (x.get("funding") or {}).get("last") or {}
        out[sym] = {"ticker": meta["omni"], "mark": float(x["mark_price"]),
                    "spread_bps": float(x.get("base_spread_bps") or meta["spread_bps"]),
                    "spread_assumed": not x.get("base_spread_bps"), "kind": meta["kind"],
                    "funding_annual_long": _f(f.get("annualized_long_rate") or f.get("annualized_rate")),
                    "funding_annual_short": _f(f.get("annualized_short_rate") or f.get("annualized_rate"))}
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Parallel oracle check: CoinGecko consensus + the venue's own index ────────
def _match(symbol: str, aliases: list[str]) -> bool:
    s = (symbol or "").upper()
    return any(re.match(r"^(?:[A-Z0-9]+:)?" + a + r"(?:[-_/ ]?(?:USDT|USDC|USDG|USD|PERP)|[-_]SWAP|$)", s) for a in aliases)


async def fetch_coingecko_consensus(client: httpx.AsyncClient) -> dict:
    """Median of the index (oracle) prices of every perp venue listing the market, after dropping stale,
    thin (< 100k USD/24h) and off-consensus (> 2% from the first-pass median) tickers."""
    r = await client.get(COINGECKO_DERIVATIVES, timeout=60)
    r.raise_for_status()
    now = time.time()
    out = {}
    rows = r.json()
    for sym, aliases in ORACLE_ALIASES.items():
        venues = []
        for x in rows:
            if x.get("contract_type") != "perpetual" or not _match(x.get("symbol"), aliases):
                continue
            try:
                idx, px, vol = float(x["index"]), float(x["price"]), float(x.get("volume_24h") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            if vol < 100_000 or not idx or now - float(x.get("last_traded_at") or 0) > 3600:
                continue
            venues.append({"market": x["market"], "symbol": x["symbol"], "price": px, "index": idx,
                           "basis": x.get("basis"), "volume": vol})
        if not venues:
            continue
        med0 = statistics.median(v["index"] for v in venues)
        venues = [v for v in venues if abs(v["index"] / med0 - 1) <= 0.02]
        idxs = [v["index"] for v in venues]
        out[sym] = {"ts": int(now), "index_med": statistics.median(idxs),
                    "price_med": statistics.median(v["price"] for v in venues), "n": len(venues),
                    "lo": min(idxs), "hi": max(idxs),
                    "venues": sorted(venues, key=lambda v: -v["volume"])[:12]}
    return out


async def fetch_venue_index(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """The chart venue's own oracle: Binance premiumIndex (index + mark) or Hyperliquid oraclePx / markPx."""
    meta = SYMBOLS[symbol]
    if meta["source"] == "binance":
        r = await client.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": meta["ticker"]}, timeout=10)
        r.raise_for_status()
        j = r.json()
        return {"index": float(j["indexPrice"]), "mark": float(j["markPrice"]), "funding": float(j["lastFundingRate"])}
    dex = meta["ticker"].split(":")[0]
    r = await client.post(HYPERLIQUID_INFO, json={"type": "metaAndAssetCtxs", "dex": dex}, timeout=10)
    r.raise_for_status()
    m, ctx = r.json()
    for u, c in zip(m["universe"], ctx):
        if u["name"] == meta["ticker"]:
            return {"index": float(c["oraclePx"]), "mark": float(c["markPx"]), "funding": float(c.get("funding") or 0)}
    return None


def save_oracle(sym: str, o: dict, venue: dict | None) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO oracle_ticks VALUES (?,?,?,?,?,?,?)",
                    (sym, o["ts"], o["index_med"], o["price_med"], o["n"],
                     venue and venue["index"], venue and venue["mark"]))


def load_oracle(symbol: str, days: int = 14) -> list[tuple]:
    with _db() as con:
        return con.execute("SELECT ts, index_med, venue_index FROM oracle_ticks WHERE symbol=? AND ts>=? ORDER BY ts",
                           (symbol, int(time.time()) - days * 86400)).fetchall()
