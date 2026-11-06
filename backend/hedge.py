"""MET (Meteora) stake hedge: size and timing of a short MET perp that protects staked MET.

Two ways, from research/met_hedge.py (Hyperliquid MET history, net of fees, spread and real funding):
  static    short met_hedge_pct % of the staked tokens all the time → price exposure ≈ 0, you keep the
            staking yield. Over Oct 2025 – Nov 2026 the stake alone fell −83% (worst −94%); hedged: +0.6%.
  4h        short only while the FVG-ICT 4h structure is bearish → keeps part of the upside, cuts the
            worst drop (−94% → −38%) but costs fees on every switch. The 1h structure flipped too often and lost.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import SYMBOLS

REPORT = Path(__file__).resolve().parent.parent / "research" / "met_hedge_report.json"
SYM = "MET"


def _report() -> dict:
    try:
        r = json.loads(REPORT.read_text())
    except (OSError, ValueError):
        return {}
    keep = ("final_pct", "max_dd_pct", "worst_7d_pct", "switches", "time_hedged_pct", "costs_pct", "funding_pct")
    out = {"generated": r.get("generated"), "runs": {}}
    for tf, run in r.get("runs", {}).items():
        out["runs"][tf] = {"from": run["from"], "to": run["to"], "price_from": run["price_from"],
                           "price_to": run["price_to"],
                           "variants": {k: {x: v.get(x) for x in keep} | {"curve": v.get("curve", [])}
                                        for k, v in run["variants"].items()}}
    return out


def _since_flip(trend, ts) -> int | None:
    """Open time of the bar where the current structure direction started."""
    if not len(trend):
        return None
    cur, i = trend[-1], len(trend) - 1
    while i > 0 and trend[i - 1] == cur:
        i -= 1
    return int(ts[i])


def snapshot(state: dict, live: dict, S: dict) -> dict | None:
    if SYM not in SYMBOLS:
        return None
    q = live.get(SYM) or {}
    st4, st1, st5 = state.get((SYM, "4h")), state.get((SYM, "1h")), state.get((SYM, "5m"))
    chart_px = float(st5["bars"]["close"].iloc[-1]) if st5 else None
    if st5 is not None and len(st5["forming"]):
        chart_px = float(st5["forming"]["close"].iloc[-1])
    px = q.get("mark") or chart_px
    tokens = float(S.get("met_staked") or 0)
    pct = float(S.get("met_hedge_pct", 100)) / 100
    lev = max(1.0, min(float(S.get("met_hedge_lev", 2)), SYMBOLS[SYM]["max_lev"]))
    mmr = S["mmr_pct"] / 100
    short_tok = tokens * pct
    notional = short_tok * px if px else 0.0
    liq = px * (1 + 1 / lev - mmr) if px else None
    fr = q.get("funding_annual_short")              # Omni: positive → longs pay, shorts receive
    fund_day = notional * fr / 365 if fr is not None else None

    def trend_of(st):
        if not st:
            return None, None
        tr = st["res"]["trend"]
        return int(tr[-1]), _since_flip(tr, st["bars"]["ts"].to_numpy())

    t4, t4_since = trend_of(st4)
    t1, _ = trend_of(st1)
    t5 = st5["res"]["table"].get("last_shift") if st5 else None
    on = t4 == -1
    return {
        "symbol": SYM, "omni": SYMBOLS[SYM]["omni"], "price": px, "price_src": "Omni mark" if q.get("mark") else "chart",
        "tokens": tokens, "stake_usd": tokens * px if px else None, "hedge_pct": pct * 100, "lev": lev,
        "short_tokens": short_tok, "notional": notional, "margin": notional / lev if notional else 0.0,
        "liq": liq, "liq_up_pct": (liq / px - 1) * 100 if liq else None,
        "fee_open": notional * (S["fee_pct"] + SYMBOLS[SYM]["spread_bps"] / 200) / 100,
        "funding_annual": fr, "funding_day": fund_day,
        "trend_4h": t4, "trend_4h_since": t4_since, "trend_1h": t1, "shift_5m": t5,
        "dynamic": {"on": on, "short_tokens": short_tok if on else 0.0, "notional": notional if on else 0.0},
        "report": _report(),
    }
