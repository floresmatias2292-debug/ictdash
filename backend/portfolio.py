"""Live position suggestion per group.

Strength per symbol (−1…+1) = blend of
  structure  per timeframe (weights 5m 1, 1h 2, 4h 3): trend (±1) + Premium/Discount only when it agrees
             with the trend (Discount in an uptrend +0.5, Premium in a downtrend −0.5) + best live setup
  live       Omni mark move over the last 60 min (in 2 × ATR5m) and 15 min (in 1 × ATR5m), clipped to ±1.
             Weighted 50% while the NY session is closed (Yahoo bars are stale), 30% while open.

Decision (one of three modes):
  TREND  the sector (mean strength of the group's sector symbols) is ≥ 0.35 in one direction and every
         sector symbol agrees. Sector momentum alone failed out-of-sample validation
         (research/trend_report.py), so TREND is context: it blocks counter-trend trades and hedges, and only
         proposes an entry when a live FVG setup that passes the validated filters points the same way.
  PAIR   no sector trend, AMD/NVDA (US100/US30) strength gap ≥ 0.25 → long the stronger, short the weaker,
         plus a beta hedge only if it does not lean against the sector (|sector| ≥ 0.15). Not backtested.
  WAIT   otherwise → no position.
Sizing: all stops together lose risk_pct of capital; margin capped at margin_pct; leverage per leg is the
lowest that fits the cap, never above the cap that keeps the liquidation beyond the stop.
"""
from __future__ import annotations

import numpy as np

from . import data
from .config import GROUPS, SYMBOLS
from .stats import liq_price, safe_leverage

TF_W = {"5m": 1, "1h": 2, "4h": 3}
TREND_MIN, PAIR_MIN, HEDGE_BLOCK = 0.35, 0.25, 0.15


def max_lev(sym: str, S: dict) -> float:
    return S["swap_max_lev"] if SYMBOLS[sym]["kind"] == "swap" else SYMBOLS[sym]["max_lev"]


def fee_pct(sym: str, S: dict) -> float:
    return S[GROUPS[SYMBOLS[sym]["group"]]["fee_key"]]


def _structure(sym: str, state: dict, S: dict) -> tuple[float, dict]:
    total, wsum, parts = 0.0, 0.0, {}
    for tf, w in TF_W.items():
        st = state.get((sym, tf))
        if not st:
            continue
        tb = st["res"]["table"]
        trend = tb.get("trend", 0)
        pdz = (tb.get("pd") or {}).get("zone")
        # P/D only counts in the trend's favour (ICT: buy discount in an uptrend, sell premium in a downtrend)
        pd_b = 0.5 if (pdz == "Discount" and trend == 1) else -0.5 if (pdz == "Premium" and trend == -1) else 0.0
        best = best_live(st["setups"], S)
        sig = best["dir"] * best["prob"] / 100 if best else 0.0
        x = (trend + pd_b + sig) / 2.5
        parts[tf] = {"trend": trend, "pd": pdz, "signal": best and {k: best[k] for k in ("dir", "prob", "score", "entry", "stop", "target", "status")}, "value": x}
        total += w * x
        wsum += w
    return (total / wsum if wsum else 0.0), parts


def live_momentum(sym: str, state: dict) -> dict:
    """Omni mark move over the last 60 and 15 minutes, scaled by the 5m ATR."""
    bars = data.load_omni_bars(sym, "5m", days=1)
    st = state.get((sym, "5m"))
    atr = (st["res"]["table"].get("atr") if st else None) or 0
    if len(bars) < 2 or atr <= 0:
        return {"value": 0.0, "move60_pct": None, "move15_pct": None, "bars": len(bars)}
    last = bars[-1]["close"]
    ref60 = bars[max(0, len(bars) - 13)]["open"]
    ref15 = bars[max(0, len(bars) - 4)]["open"]
    m60, m15 = last - ref60, last - ref15
    v = 0.6 * float(np.clip(m60 / (2 * atr), -1, 1)) + 0.4 * float(np.clip(m15 / atr, -1, 1))
    return {"value": v, "move60_pct": m60 / ref60 * 100, "move15_pct": m15 / ref15 * 100, "bars": len(bars),
            "atr5m": atr, "z60": m60 / atr}         # z60: unclipped 60-min move in ATRs (tie-break)


def strength(sym: str, state: dict, S: dict) -> dict:
    struct, parts = _structure(sym, state, S)
    live = live_momentum(sym, state)
    w = 0.3 if data.session_open() else 0.5
    if live["bars"] < 4:
        w = 0.0                                   # not enough Omni history yet
    return {"value": (1 - w) * struct + w * live["value"], "structure": struct, "live": live, "live_w": w,
            "parts": parts}


def best_live(setups: list[dict], S: dict) -> dict | None:
    live = [s for s in setups if s["status"] in ("pending", "open") and s["prob"] >= S["watch_prob"]]
    return max(live, key=lambda s: s["prob"]) if live else None


def best_validated(state: dict, symbols: list, d: int) -> dict | None:
    """Pending FVG limit order (not filled yet) that passes the out-of-sample filters and points in direction d.
    5m is skipped: its costs eat the edge (research/filter_search.py)."""
    cands = []
    for sym in symbols:
        for tf in ("4h", "1h"):
            st = state.get((sym, tf))
            for s in (st or {}).get("setups", []):
                if s["status"] == "pending" and s.get("filter_ok") and s["dir"] == d:
                    cands.append({**s, "tf": tf})
    return max(cands, key=lambda s: s["born_ts"]) if cands else None      # most recent


def _leg_from_setup(s: dict, state: dict, live: dict, S: dict) -> dict:
    sym, d = s["symbol"], s["dir"]
    entry, stop = s["entry"], s["stop"]
    R = abs(entry - stop)
    stop_pct = R / entry
    lev = safe_leverage(stop_pct, max_lev(sym, S), S)
    px = (live.get(sym) or {}).get("mark") or entry
    return {"symbol": sym, "omni": SYMBOLS[sym]["omni"], "side": "LONG" if d == 1 else "SHORT", "dir": d,
            "mark": px, "entry": entry, "stop": stop, "target": s["target"], "stop_pct": stop_pct * 100,
            "lev": lev, "liq": liq_price(entry, d, lev, S),
            "basis": f"{s['tf']} FVG limit ({s['status']}), filters ✓, walk-forward {s['prob']:.0f}%"}


def beta_vs(sym: str, ref: str, state: dict, S: dict) -> float:
    """Beta on hourly returns built from 5m closes bucketed by clock hour, so markets whose
    Yahoo 1h bars start at :00 (DX-Y.NYB) and at :30 (stocks, cash indices) still line up."""
    a, b = state.get((sym, "5m")), state.get((ref, "5m"))
    if not a or not b:
        return 0.0
    hourly = lambda st: st["bars"].assign(h=st["bars"]["ts"] // 3600).groupby("h")["close"].last()
    j = hourly(a).to_frame("a").join(hourly(b).to_frame("b"), how="inner").tail(S["beta_lookback_bars"] + 1)
    r = np.log(j).diff().dropna()
    if len(r) < 30 or r["b"].var() == 0:
        return 0.0
    return float(r.cov().loc["a", "b"] / r["b"].var())


def _leg(sym: str, d: int, state: dict, live: dict, S: dict) -> dict:
    """Entry at the Omni mark (else last close); stop from a matching live setup or 1.5 × ATR(1h)."""
    st = state[(sym, "1h")]
    px = (live.get(sym) or {}).get("mark") or float(st["bars"]["close"].iloc[-1])
    sig = None
    for tf in ("4h", "1h", "5m"):
        b = best_live(state[(sym, tf)]["setups"], S) if (sym, tf) in state else None
        if b and b["dir"] == d:
            sig = {**b, "tf": tf}
            break
    if sig:
        entry, stop, src = sig["entry"], sig["stop"], f"{sig['tf']} FVG setup ({sig['prob']:.0f}%)"
    else:
        atr = st["res"]["table"].get("atr") or px * 0.01
        entry, stop, src = px, px - d * 1.5 * atr, "1.5 × ATR(1h), no live setup"
    R = abs(entry - stop)
    stop_pct = R / entry
    lev = safe_leverage(stop_pct, max_lev(sym, S), S)
    return {"symbol": sym, "omni": SYMBOLS[sym]["omni"], "side": "LONG" if d == 1 else "SHORT", "dir": d,
            "mark": px, "entry": entry, "stop": stop, "target": entry + d * S["rr"] * R,
            "stop_pct": stop_pct * 100, "lev": lev, "liq": liq_price(entry, d, lev, S), "basis": src}


def suggest(group: str, state: dict, live: dict, S: dict) -> dict:
    g = GROUPS[group]
    PAIR, HEDGE = g["pair"], g["hedge"]
    st = {s: strength(s, state, S) for s in g["symbols"]}
    sector_syms = g["sector"]
    sector = float(np.mean([st[s]["value"] for s in sector_syms]))
    agree = len({np.sign(round(st[s]["value"], 3)) for s in sector_syms}) == 1
    lo_, hi_ = sorted(PAIR, key=lambda s: st[s]["value"])
    edge = st[hi_]["value"] - st[lo_]["value"]
    reasons, legs, hedge_leg, b_long = [], [], None, None
    b_short, resid = None, 0.0
    if abs(sector) >= TREND_MIN and agree:
        mode, d = "TREND", (1 if sector > 0 else -1)
        word = "bullish" if d == 1 else "bearish"
        top = max(d * st[s]["value"] for s in sector_syms)
        close = [s for s in sector_syms if d * st[s]["value"] >= top - 0.05]
        pick = max(close, key=lambda s: d * (st[s]["live"].get("z60") or 0))
        mv = st[pick]["live"].get("move60_pct")
        reasons.append(f"Sector {word}: mean strength {sector:+.2f}, all of {', '.join(sector_syms)} agree; "
                       f"most extreme {pick} {st[pick]['value']:+.2f}" + (f" (Omni {mv:+.2f}% in 60 min)." if mv is not None else "."))
        reasons.append("Sector momentum alone has no validated edge (out-of-sample backtest below), so it is "
                       "not an entry by itself. Counter-trend trades and hedges are blocked.")
        setup = best_validated(state, sector_syms, d)
        if setup:
            legs = [_leg_from_setup(setup, state, live, S)]
            reasons.append(f"Entry from a validated setup in the trend direction: {setup['symbol']} {setup['tf']} FVG "
                           f"{setup['score']}/5 (passes cost and session filters), limit at {setup['entry']:.2f}.")
        else:
            reasons.append(f"No validated FVG setup points {word} right now → wait for one (1h setups closing ≥14:00 ET "
                           f"with costs ≤ {S.get('max_cost_r', 0.25)}R, or 4h setups with costs in range).")
    elif edge >= PAIR_MIN:
        mode = "PAIR"
        legs = [_leg(hi_, 1, state, live, S), _leg(lo_, -1, state, live, S)]
        b_long, b_short = beta_vs(hi_, HEDGE, state, S), beta_vs(lo_, HEDGE, state, S)
        resid = b_long - b_short                # hedge-market dollars per 1 USD of pair notional
        hedge_dir = -1 if resid > 0 else 1
        reasons.append(f"{hi_} {st[hi_]['value']:+.2f} vs {lo_} {st[lo_]['value']:+.2f}: gap {edge:.2f} ≥ {PAIR_MIN}. "
                       "Pair mode is not backtested yet — treat it as a hypothesis.")
        if abs(resid) > 0.02 and not (abs(sector) >= HEDGE_BLOCK and np.sign(sector) != hedge_dir):
            hedge_leg = _leg(HEDGE, hedge_dir, state, live, S)
            hedge_leg["role"] = "hedge"
            legs.append(hedge_leg)
            reasons.append(f"{HEDGE} {'short' if hedge_dir == -1 else 'long'} offsets the pair's leftover beta ({resid:+.2f}).")
        elif abs(resid) > 0.02:
            reasons.append(f"{HEDGE} hedge skipped: it would be {'long' if hedge_dir == 1 else 'short'} against a "
                           f"{'bearish' if sector < 0 else 'bullish'} sector ({sector:+.2f}).")
    else:
        mode = "WAIT"
        why = []
        if abs(sector) < TREND_MIN:
            why.append(f"sector {sector:+.2f} (needs ±{TREND_MIN})")
        elif not agree:
            why.append("sector symbols disagree in direction")
        why.append(f"{hi_}/{lo_} gap {edge:.2f} (needs {PAIR_MIN})")
        reasons.append("No position: " + " · ".join(why) + ".")

    for leg in legs:
        leg.setdefault("role", "main")
    margin_cap = S["capital"] * S["margin_pct"] / 100
    risk_budget = S["capital"] * S["risk_pct"] / 100
    N = margin_total = worst = 0.0
    capped_by = None
    if legs:
        cost = 2 * (fee_pct(legs[0]["symbol"], S) + S["slippage_pct"]) / 100
        w = {id(l): (abs(resid) if l is hedge_leg else 1.0) for l in legs}
        N_risk = risk_budget / sum(w[id(l)] * (l["stop_pct"] / 100 + cost) for l in legs)
        N_margin = margin_cap / sum(w[id(l)] / l["lev"] for l in legs)
        N = min(N_risk, N_margin)
        capped_by = "margin" if N_margin < N_risk else "risk"
        total_notional = N * sum(w.values())
        need = max(1, int(np.ceil(total_notional / margin_cap))) if margin_cap > 0 else 1
        while need < 50 and sum(N * w[id(l)] / min(l["lev"], need) for l in legs) > margin_cap * 1.0001:
            need += 1
        for leg in legs:
            leg["lev"] = min(leg["lev"], need)
            leg["liq"] = liq_price(leg["entry"], leg["dir"], leg["lev"], S)
            leg["notional"] = N * w[id(leg)]
            leg["margin"] = leg["notional"] / leg["lev"]
            leg["qty"] = leg["notional"] / leg["entry"]
            leg["risk_usdc"] = leg["notional"] * (leg["stop_pct"] / 100 + cost)
        margin_total = sum(l["margin"] for l in legs)
        worst = sum(l["risk_usdc"] for l in legs)
    verdict = {"WAIT": "WAIT — no position",
               "TREND": (" ".join(f"{l['side']} {l['symbol']}" for l in legs) + " · validated setup" if legs else
                         f"Sector {'bullish' if sector > 0 else 'bearish'} — no validated entry, wait"),
               "PAIR": " / ".join(f"{l['side']} {l['symbol']}" for l in legs if l["role"] == "main")
                       + (f" + {hedge_leg['side']} {HEDGE} hedge" if hedge_leg else "")}[mode]
    return {"mode": mode, "verdict": verdict, "reasons": reasons, "sector": sector, "sector_symbols": sector_syms,
            "edge": edge, "strength": st, "beta": ({hi_: b_long, lo_: b_short} if b_long is not None else {}),
            "resid_beta": resid, "legs": legs, "margin_total": margin_total, "margin_cap": margin_cap,
            "pair_notional": N, "capped_by": capped_by, "worst_case_usdc": worst,
            "worst_case_pct": worst / S["capital"] * 100, "hedge": HEDGE, "pair": list(PAIR),
            "session_open": data.session_open(), "best_single": _best_single(state, S, g["symbols"])}


def _best_single(state: dict, S: dict, symbols: list) -> dict | None:
    cands = []
    for (sym, tf), st in state.items():
        if sym not in symbols:
            continue
        b = best_live(st["setups"], S)
        if b:
            cands.append({**{k: b[k] for k in ("dir", "prob", "prob_n", "score", "entry", "stop", "target", "status")},
                          "symbol": sym, "tf": tf})
    return max(cands, key=lambda x: x["prob"]) if cands else None
