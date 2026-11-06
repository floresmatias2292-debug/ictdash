"""MET (Meteora) stake hedge: does shorting the MET perp protect a staked MET position, and at what cost?

The staked tokens cannot be sold quickly, so the hedge is a short perp on the same token. Compared on the
Hyperliquid MET history (1h bars; 4h for the long view), per 1 MET staked:

  none        hold the stake only
  static      short 100% of the tokens all the time (price exposure ≈ 0; you keep the staking yield)
  trend_1h    short 100% only while the FVG-ICT 1h structure is bearish (decided at the bar close)
  trend_4h    same with the 4h structure
  either      short while the 1h OR the 4h structure is bearish (more protective, more switches)

Costs: taker fee + half the Omni spread + slippage on every open / close of the short, and the real hourly
Hyperliquid funding (a short receives it when positive). Staking yield is not modelled (it is the same in
every variant). No lookahead: a signal from bar t is traded at bar t's close and earns from bar t+1.

Run: .venv/bin/python -W ignore research/met_hedge.py      → research/met_hedge_report.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import data, ict  # noqa: E402
from backend.config import HYPERLIQUID_INFO, SETTINGS, SYMBOLS  # noqa: E402

OUT = Path(__file__).resolve().parent / "met_hedge_report.json"
COST = (SETTINGS["fee_pct"] + SYMBOLS["MET"]["spread_bps"] / 100 / 2 + SETTINGS["slippage_pct"]) / 100   # per side


def funding_history(start_ms: int) -> dict[int, float]:
    """Hourly Hyperliquid funding rate (per hour, decimal) keyed by the hour (unix s)."""
    out, t = {}, start_ms
    with httpx.Client() as c:
        while True:
            r = c.post(HYPERLIQUID_INFO, json={"type": "fundingHistory", "coin": "MET", "startTime": t}, timeout=20)
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for x in rows:
                out[int(x["time"]) // 1000 // 3600 * 3600] = float(x["fundingRate"])
            nt = int(rows[-1]["time"]) + 1
            if len(rows) < 500 or nt <= t:
                break
            t = nt
            time.sleep(0.2)
    return out


def htf_trend_on(ts_low: np.ndarray, tf_low: str, bars_h, trend_h, tf_h: str) -> np.ndarray:
    """Higher-timeframe trend known at each lower-timeframe bar close (last closed HTF bar)."""
    ends = np.array([data.bar_end(int(t), tf_h) for t in bars_h["ts"]])
    when = np.array([data.bar_end(int(t), tf_low) for t in ts_low])
    idx = np.searchsorted(ends, when, side="right") - 1
    return np.where(idx >= 0, trend_h[np.clip(idx, 0, None)], 0)


def simulate(close: np.ndarray, ts: np.ndarray, hedge: np.ndarray, fund: dict[int, float], tf_h: float) -> dict:
    """Value of 1 staked MET + short `hedge[t]` MET (0 or 1) over time. hedge[t] is decided at close t."""
    n = len(close)
    val = np.empty(n)
    val[0] = close[0]
    pnl_short = cost = fund_rcv = 0.0
    switches = 0
    h_prev = 0.0
    for i in range(1, n):
        h = hedge[i - 1]                                  # position held during bar i
        if h != h_prev:
            cost += abs(h - h_prev) * close[i - 1] * COST
            switches += 1
        pnl_short += -h * (close[i] - close[i - 1])
        # funding: shorts receive rate × notional each hour held (sum the hours inside this bar)
        hours = int(round(tf_h))
        r = sum(fund.get(int(ts[i - 1]) // 3600 * 3600 + 3600 * k, 0.0) for k in range(hours))
        fund_rcv += h * close[i - 1] * r
        val[i] = close[i] + pnl_short + fund_rcv - cost
        h_prev = h
    peak = np.maximum.accumulate(val)
    dd = (val / peak - 1).min()
    w = max(1, int(round(7 * 24 / tf_h)))
    worst7 = min((val[i] / val[i - w] - 1) for i in range(w, n)) if n > w else None
    return {"final_pct": (val[-1] / val[0] - 1) * 100, "max_dd_pct": dd * 100,
            "worst_7d_pct": None if worst7 is None else worst7 * 100, "switches": switches,
            "time_hedged_pct": float(hedge[:-1].mean() * 100), "costs_pct": cost / close[0] * 100,
            "funding_pct": fund_rcv / close[0] * 100, "curve": val}


def run(tf: str, htf: str | None) -> dict:
    b = data.load_bars("MET", tf)
    b = b[b["closed"]].reset_index(drop=True)
    res = ict.run(b, tf)
    tr = np.asarray(res["trend"])
    close, ts = b["close"].to_numpy(float), b["ts"].to_numpy()
    fund = funding_history(int(ts[0]) * 1000)
    tf_h = {"5m": 1 / 12, "15m": 0.25, "1h": 1, "4h": 4}[tf]
    variants = {"none": np.zeros(len(b)), "static": np.ones(len(b)), f"trend_{tf}": (tr == -1).astype(float)}
    if htf:
        hb = data.load_bars("MET", htf)
        hb = hb[hb["closed"]].reset_index(drop=True)
        htr = htf_trend_on(ts, tf, hb, np.asarray(ict.run(hb, htf)["trend"]), htf)
        variants[f"trend_{htf}"] = (htr == -1).astype(float)
        variants["either"] = ((tr == -1) | (htr == -1)).astype(float)
    out = {"tf": tf, "from": int(ts[0]), "to": int(ts[-1]), "bars": len(b), "price_from": close[0],
           "price_to": close[-1], "funding_hours": len(fund), "cost_per_side_pct": COST * 100, "variants": {}}
    half = len(b) // 2
    for k, h in variants.items():
        full = simulate(close, ts, h, fund, tf_h)
        a = simulate(close[:half], ts[:half], h[:half], fund, tf_h)
        z = simulate(close[half:], ts[half:], h[half:], fund, tf_h)
        curve = full.pop("curve")
        step = max(1, len(curve) // 200)
        out["variants"][k] = {**full, "first_half": {x: a[x] for x in ("final_pct", "max_dd_pct")},
                              "second_half": {x: z[x] for x in ("final_pct", "max_dd_pct")},
                              "curve": [[int(ts[i]), round(float(curve[i] / curve[0]), 4)] for i in range(0, len(curve), step)]}
    return out


if __name__ == "__main__":
    rep = {"generated": int(time.time()), "runs": {"1h": run("1h", "4h"), "4h": run("4h", None)}}
    OUT.write_text(json.dumps(rep, default=float))
    for name, r in rep["runs"].items():
        print(f"\n{name}: {r['bars']} bars {time.strftime('%Y-%m-%d', time.gmtime(r['from']))} → "
              f"{time.strftime('%Y-%m-%d', time.gmtime(r['to']))} · MET {r['price_from']:.4f} → {r['price_to']:.4f}"
              f" · cost/side {r['cost_per_side_pct']:.3f}% · funding hours {r['funding_hours']}")
        print(f"  {'variant':<10} {'final':>8} {'max DD':>8} {'worst 7d':>9} {'hedged':>7} {'switch':>6} {'costs':>6} {'fund':>6}  1st half / 2nd half final")
        for k, v in r["variants"].items():
            print(f"  {k:<10} {v['final_pct']:+7.1f}% {v['max_dd_pct']:+7.1f}% {v['worst_7d_pct']:+8.1f}% {v['time_hedged_pct']:6.0f}%"
                  f" {v['switches']:6d} {v['costs_pct']:5.1f}% {v['funding_pct']:+5.1f}%  "
                  f"{v['first_half']['final_pct']:+.1f}% / {v['second_half']['final_pct']:+.1f}%")
