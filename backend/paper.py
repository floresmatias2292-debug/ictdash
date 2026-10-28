"""Paper trading on the futures chart: fictitious long / short with a fixed leverage and margin.

Every trade uses PAPER["lev"] and PAPER["margin"] (x15, 12 USDC → 180 USDC notional). It opens and closes at
the latest futures price (Binance / Hyperliquid, the chart's own feed), pays the market's taker fee on each
side, and is liquidated when the price touches the liquidation level (the whole margin is lost).
Funding is not charged. Trades are kept in SQLite, so they survive restarts.
"""
from __future__ import annotations

import time

from . import data
from .config import PAPER, SETTINGS, SYMBOLS
from .portfolio import fee_pct
from .stats import liq_price


def _db():
    con = data._db()
    con.execute("CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side INTEGER,"
                " lev REAL, margin REAL, notional REAL, entry REAL, entry_ts INTEGER, liq REAL, fee_rate REAL,"
                " exit REAL, exit_ts INTEGER, pnl REAL, status TEXT, reason TEXT)")
    return con


COLS = ["id", "symbol", "side", "lev", "margin", "notional", "entry", "entry_ts", "liq", "fee_rate", "exit", "exit_ts",
        "pnl", "status", "reason"]


def last_price(sym: str) -> float | None:
    b = data.load_bars(sym, "5m", 1)
    return float(b["close"].iloc[-1]) if len(b) else None


def _rows(where: str, args=(), limit: int = 200) -> list[dict]:
    with _db() as con:
        rows = con.execute(f"SELECT {','.join(COLS)} FROM paper_trades WHERE {where} ORDER BY id DESC LIMIT ?",
                           (*args, limit)).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def pnl(t: dict, price: float) -> dict:
    """Open trade marked to `price`: gross move, fees (open paid + close estimated), net."""
    gross = t["side"] * (price - t["entry"]) / t["entry"] * t["notional"]
    fees = t["fee_rate"] * t["notional"] * (1 + price / t["entry"])
    net = gross - fees
    return {"price": price, "gross": gross, "fees": fees, "net": net, "roe": net / t["margin"] * 100}


def state() -> dict:
    opn, closed = _rows("status='open'"), _rows("status!='open'", limit=50)
    for t in opn:
        px = last_price(t["symbol"])
        t["mark"] = pnl(t, px) if px else None
    with _db() as con:
        realized, n, wins = con.execute("SELECT COALESCE(SUM(pnl),0), COUNT(*), COALESCE(SUM(pnl>0),0) FROM paper_trades"
                                        " WHERE status!='open'").fetchone()
    used = sum(t["margin"] for t in opn)
    balance = SETTINGS["capital"] + realized
    return {"open": opn, "closed": closed, "balance": balance, "free": balance - used, "realized": realized,
            "n_closed": n, "wins": wins, "cfg": PAPER, "start": SETTINGS["capital"]}


def open_trade(sym: str, side: int) -> dict:
    if sym not in SYMBOLS or side not in (1, -1):
        raise ValueError("unknown symbol or side")
    px = last_price(sym)
    if not px:
        raise ValueError(f"no futures price for {sym}")
    lev, margin = PAPER["lev"], PAPER["margin"]
    notional = margin * lev
    rate = fee_pct(sym, SETTINGS) / 100
    st = state()
    if st["free"] < margin + rate * notional:
        raise ValueError(f"not enough free paper balance ({st['free']:.2f} USDC)")
    liq = liq_price(px, side, lev, SETTINGS)
    with _db() as con:
        cur = con.execute("INSERT INTO paper_trades (symbol, side, lev, margin, notional, entry, entry_ts, liq, fee_rate, status)"
                          " VALUES (?,?,?,?,?,?,?,?,?, 'open')", (sym, side, lev, margin, notional, px, int(time.time()), liq, rate))
        return {"id": cur.lastrowid, "entry": px, "liq": liq}


def close_trade(tid: int, price: float | None = None, reason: str = "manual") -> dict:
    t = next(iter(_rows("id=? AND status='open'", (tid,))), None)
    if not t:
        raise ValueError("trade not open")
    px = price or last_price(t["symbol"])
    if reason == "liquidated":
        net = -t["margin"] - t["fee_rate"] * t["notional"]       # the whole margin plus the opening fee
    else:
        net = pnl(t, px)["net"]
    with _db() as con:
        con.execute("UPDATE paper_trades SET exit=?, exit_ts=?, pnl=?, status='closed', reason=? WHERE id=?",
                    (px, int(time.time()), net, reason, tid))
    return {"id": tid, "exit": px, "pnl": net, "reason": reason}


def check_liquidations(last: dict) -> list[dict]:
    """last: symbol -> latest futures price (polled every 5 s). Closes trades whose liquidation price was hit."""
    out = []
    for t in _rows("status='open'"):
        px = last.get(t["symbol"])
        if px is None:
            continue
        if (t["side"] == 1 and px <= t["liq"]) or (t["side"] == -1 and px >= t["liq"]):
            out.append(close_trade(t["id"], t["liq"], "liquidated"))
    return out
