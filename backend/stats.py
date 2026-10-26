"""Setup outcomes, walk-forward probability and the leveraged backtest.

Setup rules (one per FVG from the Pine port):
  entry  = limit at the FVG's near edge (bull: top, bear: bottom)
  stop   = far edge ± stop_atr_buffer × ATR
  target = entry ± rr × R
  The order waits `horizon` bars to fill; it is cancelled if price reaches the target first.
  After the fill it has `horizon` bars to hit target or stop, else it exits at that bar's close.
  When one bar touches both stop and target, the stop wins (conservative).

Probability: win rate of past setups with the same (timeframe, direction, score), pooled across
symbols, counting only setups already resolved at that moment (no lookahead). Small buckets are
shrunk toward the timeframe's overall win rate.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")


def simulate_setups(bars, res: dict, tf: str, symbol: str, S: dict) -> list[dict]:
    ts = bars["ts"].to_numpy(np.int64)
    o, h, l, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    H = S["horizon"][tf]
    out = []
    for z in res["fvgs"]:
        d = 1 if z["bull"] else -1
        atr = z["atr"]
        entry = z["top"] if z["bull"] else z["bot"]
        stop = (z["bot"] - S["stop_atr_buffer"] * atr) if z["bull"] else (z["top"] + S["stop_atr_buffer"] * atr)
        R = abs(entry - stop)
        target = entry + d * S["rr"] * R
        s = {"symbol": symbol, "tf": tf, "fvg": z["id"], "dir": d, "score": z["score"], "comps": z["comps"],
             "born": z["born"], "born_ts": int(ts[z["born"]]), "entry": entry, "stop": stop, "target": target,
             "R": R, "status": "pending", "fill": None, "exit": None, "exit_px": None, "r": None,
             "resolve_ts": None}
        b = z["born"]
        # fill window
        j = b + 1
        while j < n and j <= b + H:
            reached_entry = l[j] <= entry if d == 1 else h[j] >= entry
            ran_away = h[j] >= target if d == 1 else l[j] <= target
            if reached_entry:
                break
            if ran_away:
                s.update(status="missed", resolve_ts=int(ts[j]))
                break
            j += 1
        if s["status"] == "missed":
            out.append(s)
            continue
        if j > b + H:
            s.update(status="expired", resolve_ts=int(ts[min(j, n - 1)]))
            out.append(s)
            continue
        if j >= n:                     # still waiting for a fill
            out.append(s)
            continue
        s["fill"] = j
        s["fill_px"] = entry if (o[j] - entry) * d >= 0 else o[j]  # gap through the limit fills at the open
        # resolution window (fill bar can only stop out)
        k = j
        while k < n and k <= j + H:
            hit_stop = l[k] <= stop if d == 1 else h[k] >= stop
            hit_tgt = (h[k] >= target if d == 1 else l[k] <= target) and k > j
            if hit_stop:
                px = stop if (o[k] - stop) * d > 0 or k == j else o[k]   # gap through the stop fills at the open
                s.update(status="loss", exit=k, exit_px=px)
                break
            if hit_tgt:
                px = target if (target - o[k]) * d > 0 else o[k]
                s.update(status="win", exit=k, exit_px=px)
                break
            k += 1
        if s["status"] == "pending":
            if j + H < n:
                k = j + H
                s.update(status="timeout", exit=k, exit_px=c[k])
            else:
                s["status"] = "open"
        if s["exit"] is not None:
            s["resolve_ts"] = int(ts[s["exit"]])
            s["r"] = (s["exit_px"] - s["fill_px"]) * d / R if R > 0 else 0.0
        out.append(s)
    return out


def add_probabilities(setups: list[dict], S: dict) -> None:
    """Walk-forward probability for every setup of one timeframe (all symbols together)."""
    m = S["min_samples"]
    traded = [s for s in setups if s["status"] in ("win", "loss", "timeout")]
    resolved = sorted(traded, key=lambda s: s["resolve_ts"])
    bucket = defaultdict(lambda: [0, 0])       # (dir, score) -> [wins, n]
    overall = [0, 0]
    ri = 0
    for s in sorted(setups, key=lambda s: s["born_ts"]):
        while ri < len(resolved) and resolved[ri]["resolve_ts"] < s["born_ts"]:
            r = resolved[ri]
            w = 1 if r["status"] == "win" else 0
            bucket[(r["dir"], r["score"])][0] += w
            bucket[(r["dir"], r["score"])][1] += 1
            overall[0] += w
            overall[1] += 1
            ri += 1
        prior = (overall[0] + 1) / (overall[1] + 3)           # ~33% with no data (a 2R coin-flip)
        w, k = bucket[(s["dir"], s["score"])]
        s["prob"] = 100 * (w + m * prior) / (k + m)
        s["prob_n"] = k


def annotate_filters(setups: list[dict], tf: str, rt_cost: float, S: dict) -> None:
    """Flag each setup with the out-of-sample filters (see research/filter_search.py):
    cost_ok  round-trip costs ≤ max_cost_r × R (stop wide enough to be worth trading)
    late     setup bar closes at/after 14:00 ET (used on 1h when late_only_1h = 1)."""
    from .data import bar_end
    for s in setups:
        entry = s["entry"]
        stop_pct = abs(entry - s["stop"]) / entry if entry else 0
        s["cost_r"] = rt_cost / stop_pct if stop_pct > 0 else float("inf")
        t = datetime.fromtimestamp(bar_end(s["born_ts"], tf), ET)
        s["late"] = t.hour * 60 + t.minute >= 14 * 60
        ok = True
        if S.get("max_cost_r", 0) > 0 and s["cost_r"] > S["max_cost_r"]:
            ok = False
        if tf == "1h" and S.get("late_only_1h") and not s["late"]:
            ok = False
        s["filter_ok"] = ok


def bucket_table(setups: list[dict]) -> list[dict]:
    """Full-sample win rate per (dir, score) — what the walk-forward estimate converges to."""
    b = defaultdict(lambda: {"n": 0, "w": 0, "r": 0.0})
    for s in setups:
        if s["status"] in ("win", "loss", "timeout"):
            x = b[(s["dir"], s["score"])]
            x["n"] += 1
            x["w"] += s["status"] == "win"
            x["r"] += s["r"]
    rows = []
    for (d, sc), x in sorted(b.items(), key=lambda kv: (-kv[0][0], kv[0][1])):
        rows.append({"dir": d, "score": sc, "n": x["n"], "win_pct": 100 * x["w"] / x["n"],
                     "avg_r": x["r"] / x["n"]})
    return rows


# ── leverage helpers ────────────────────────────────────────────────────────
def safe_leverage(stop_pct: float, max_lev: float, S: dict) -> int:
    """Highest leverage ≤ max_lev whose liquidation sits beyond the stop with a safety buffer:
    stop% ≤ buffer × (1/lev − mmr)."""
    mmr = S["mmr_pct"] / 100
    lev = 1 / (stop_pct / S["liq_buffer"] + mmr) if stop_pct > 0 else max_lev
    return int(max(1, min(max_lev, math.floor(lev))))


def liq_price(entry: float, d: int, lev: float, S: dict) -> float:
    mmr = S["mmr_pct"] / 100
    return entry * (1 - d * (1 / lev - mmr))


def costs_pct(S: dict, spread_bps: float, fee_pct: float | None = None) -> float:
    """Round-trip cost as a fraction of notional: fees + slippage + spread, both sides."""
    fee = S["fee_pct"] if fee_pct is None else fee_pct
    return 2 * (fee + S["slippage_pct"]) / 100 + spread_bps / 10000


def funding_frac(mkt: dict, d: int, entry_ts: int, exit_ts: int, S: dict) -> float:
    """Funding paid (+) or received (−) as a fraction of notional, at today's Omni rate.
    perp: accrues continuously (annualized rate × holding time; + means longs pay shorts).
    swap: one charge per 17:00 ET close held through, ×3 on Fridays; rates are per side
          (longs pay the long rate; shorts receive the short rate, or pay it if shorts_receive=0)."""
    if not S.get("funding"):
        return 0.0
    if mkt["kind"] == "perp":
        rate = mkt.get("f_long") or 0.0
        return d * rate * (exit_ts - entry_ts) / (365 * 86400)
    days = 0
    t = datetime.fromtimestamp(entry_ts, ET).replace(hour=17, minute=0, second=0, microsecond=0)
    if t.timestamp() <= entry_ts:
        t += timedelta(days=1)
    while t.timestamp() <= exit_ts:
        if t.weekday() < 5:
            days += 3 if t.weekday() == 4 else 1
        t += timedelta(days=1)
    if d == 1:
        rate = mkt.get("f_long") or 0.0
    else:
        rate = (mkt.get("f_short") or 0.0) * (-1 if S.get("shorts_receive", 1) else 1)
    return rate / 360 * days


# ── backtest ────────────────────────────────────────────────────────────────
def position_size(eq: float, entry: float, stop: float, max_lev: float, S: dict, rt_cost: float,
                  sizing: str) -> tuple[float, float, int]:
    """(notional, margin, leverage) for one trade.
    sizing="risk":   notional so that a stop-out (plus round-trip costs) loses risk_pct of equity;
                     margin is capped at margin_pct of equity (then notional shrinks).
    sizing="margin": margin = margin_pct of equity, notional = margin × leverage."""
    stop_pct = abs(entry - stop) / entry
    lev = safe_leverage(stop_pct, max_lev, S)
    cap_margin = eq * S["margin_pct"] / 100
    if sizing == "risk":
        notional = eq * S["risk_pct"] / 100 / (stop_pct + rt_cost) if stop_pct + rt_cost > 0 else 0.0
        notional = min(notional, cap_margin * lev)
        lev = max(1, min(lev, math.ceil(notional / cap_margin))) if cap_margin > 0 else lev  # least leverage that fits
        margin = notional / lev
    else:
        margin = cap_margin
        notional = margin * lev
    return notional, margin, lev


def backtest(setups: list[dict], ts: np.ndarray, mkt: dict, S: dict, min_prob: float,
             sizing: str = "risk") -> dict:
    """mkt: {max_lev, fee_pct, spread_bps, kind, f_long, f_short} for the market being tested."""
    max_lev = mkt["max_lev"]
    eq = S["capital"]
    curve = [(int(ts[0]) if len(ts) else 0, eq)]
    trades = []
    busy_until = -1
    rt_cost = costs_pct(S, mkt["spread_bps"], mkt["fee_pct"])
    for s in sorted(setups, key=lambda s: (s["fill"] if s["fill"] is not None else 10**12)):
        if s["status"] not in ("win", "loss", "timeout") or s["prob"] < min_prob:
            continue
        if s["fill"] <= busy_until or eq < 1:
            continue
        d = s["dir"]
        entry = s["fill_px"]
        notional, margin, lev = position_size(eq, entry, s["stop"], max_lev, S, rt_cost, sizing)
        move = (s["exit_px"] - entry) * d / entry
        liq_move = -(1 / lev - S["mmr_pct"] / 100)
        liquidated = move <= liq_move
        fund = notional * funding_frac(mkt, d, int(ts[s["fill"]]), int(ts[s["exit"]]), S)
        pnl = -margin if liquidated else notional * move - notional * rt_cost - fund
        pnl = max(pnl, -margin)
        eq += pnl
        busy_until = s["exit"]
        trades.append({"symbol": s["symbol"], "tf": s["tf"], "dir": d, "entry_ts": int(ts[s["fill"]]),
                       "exit_ts": int(ts[s["exit"]]), "entry": entry, "exit": s["exit_px"], "lev": lev,
                       "margin": margin, "notional": notional, "funding": -fund, "pnl": pnl, "r": s["r"], "status": "liquidated" if liquidated else s["status"],
                       "prob": s["prob"], "score": s["score"], "equity": eq})
        curve.append((int(ts[s["exit"]]), eq))
    return {"trades": trades, "curve": curve, "summary": summarize(trades, S["capital"], eq)}


def summarize(trades: list[dict], start: float, end: float) -> dict:
    if not trades:
        return {"trades": 0, "win_pct": None, "pf": None, "ret_pct": 0.0, "max_dd_pct": 0.0,
                "avg_r": None, "end_equity": end}
    wins = [t for t in trades if t["pnl"] > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    peak, dd = start, 0.0
    for t in trades:
        peak = max(peak, t["equity"])
        dd = max(dd, (peak - t["equity"]) / peak * 100 if peak > 0 else 0)
    return {"trades": len(trades), "win_pct": 100 * len(wins) / len(trades),
            "pf": gross_w / gross_l if gross_l > 0 else None, "ret_pct": (end / start - 1) * 100,
            "max_dd_pct": dd, "avg_r": float(np.mean([t["r"] for t in trades])), "end_equity": end,
            "liquidations": sum(t["status"] == "liquidated" for t in trades),
            "funding": sum(t["funding"] for t in trades)}
