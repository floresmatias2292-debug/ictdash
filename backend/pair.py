"""Delta-neutral pair: long one oil benchmark, short the other, so the oil price itself cancels out.

Spread   s = log(A) − β·log(B)   (A = WTI / USOIL, B = Brent / UKOIL)
Hedge β  "beta":   rolling min-variance ratio of A's returns on B's returns (notional B = β × notional A),
                   so the pair's sensitivity to a move in oil is ≈ 0  → the most neutral trade.
         "dollar": β = 1, equal notional on both legs.
z-score  (s − rolling mean) / rolling std over `window` bars.
Rules    mode "revert": z ≥ +z_in → spread rich: SHORT A / LONG B; z ≤ −z_in → LONG A / SHORT B.
                        Target when z returns to z_out, stop when |z| ≥ z_stop.
         mode "trend":  the opposite side (ride the stretch): target at |z| ≥ z_stop, stop back at z_out.
         Time stop after max_hold bars in both.
Statistics come from the Binance perps (CL, BZ); the trade is placed on the Omni swaps of the same oil.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data

DEFAULT = {"tf": "1h", "window": 192, "z_in": 2.0, "z_out": 0.0, "z_stop": 3.5, "max_hold": 96, "hedge": "beta"}


def aligned(proxy_a: str, proxy_b: str, tf: str, limit: int | None = None) -> pd.DataFrame:
    """Closed bars of both legs on the same timestamps."""
    a = data.load_bars(proxy_a, tf, limit)
    b = data.load_bars(proxy_b, tf, limit)
    a, b = a[a["closed"]], b[b["closed"]]
    df = a[["ts", "close"]].merge(b[["ts", "close"]], on="ts", suffixes=("_a", "_b"))
    return df.sort_values("ts").reset_index(drop=True)


def spread_stats(df: pd.DataFrame, window: int, hedge: str = "beta") -> pd.DataFrame:
    """Per bar: β, spread, z-score, half-life proxy. Uses only data up to that bar (no lookahead)."""
    la, lb = np.log(df["close_a"].to_numpy(float)), np.log(df["close_b"].to_numpy(float))
    ra, rb = pd.Series(np.diff(la, prepend=np.nan)), pd.Series(np.diff(lb, prepend=np.nan))
    if hedge == "beta":
        cov = ra.rolling(window, min_periods=window // 2).cov(rb)
        var = rb.rolling(window, min_periods=window // 2).var()
        beta = (cov / var).clip(0.3, 2.0).to_numpy()
    else:
        beta = np.ones(len(la))
    # β is fixed per bar when the z-score is read, so the spread history is rebuilt with today's β
    # (rolling mean/std of la − β_t·lb over the window, computed in closed form).
    s_a, s_b = pd.Series(la), pd.Series(lb)
    m_a, m_b = s_a.rolling(window).mean(), s_b.rolling(window).mean()
    v_a, v_b = s_a.rolling(window).var(), s_b.rolling(window).var()
    c_ab = s_a.rolling(window).cov(s_b)
    bt = pd.Series(beta)
    mean = (m_a - bt * m_b).to_numpy()
    var_s = (v_a + bt ** 2 * v_b - 2 * bt * c_ab).to_numpy()
    sd = np.sqrt(np.where(var_s > 0, var_s, np.nan))
    s = la - beta * lb
    z = (s - mean) / sd
    corr = ra.rolling(window, min_periods=window // 2).corr(rb).to_numpy()
    return pd.DataFrame({"ts": df["ts"].to_numpy(), "a": df["close_a"].to_numpy(float), "b": df["close_b"].to_numpy(float),
                         "beta": beta, "spread": s, "mean": mean, "sd": sd, "z": z, "corr": corr})


def simulate(st: pd.DataFrame, p: dict, cost: float) -> list[dict]:
    """Trades on the spread. Return per trade = P&L / notional of leg A, net of `cost` (round trip, both legs).
    Leg B's notional is β × leg A's, so P&L = d·(Δlog A − β·Δlog B) with β frozen at entry."""
    z, la, lb, beta, ts = (st[k].to_numpy(float) for k in ("z", "a", "b", "beta", "ts"))
    la, lb = np.log(la), np.log(lb)
    trades, i, n = [], 0, len(z)
    while i < n - 1:
        if np.isnan(z[i]) or abs(z[i]) < p["z_in"]:
            i += 1
            continue
        trend = p.get("mode") == "trend"
        side = 1 if z[i] > 0 else -1                  # which side of the mean the spread is on
        d = side if trend else -side                  # d = +1: long A / short B
        e, bt = i + 1, beta[i]                        # fill at the next bar (no same-bar lookahead)
        j, why = e, "time"
        while j < min(n - 1, e + p["max_hold"]):
            zj = z[j]
            if not np.isnan(zj):
                back = side * zj <= p["z_out"]        # spread returned to the mean
                far = side * zj >= p["z_stop"]        # spread kept stretching
                if back or far:
                    why = ("stop" if back else "target") if trend else ("target" if back else "stop")
                    break
            j += 1
        gross = d * ((la[j] - la[e]) - bt * (lb[j] - lb[e]))
        trades.append({"entry_ts": int(ts[e]), "exit_ts": int(ts[j]), "dir": d, "beta": float(bt), "z": float(z[i]),
                       "bars": int(j - e), "gross": float(gross), "net": float(gross - cost * (1 + bt) / 2), "exit": why})
        i = j + 1
    return trades


def summary(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    r = np.array([t["net"] for t in trades])
    out = {"n": len(r), "hit": float((r > 0).mean() * 100), "mean_pct": float(r.mean() * 100), "sum_pct": float(r.sum() * 100),
           "avg_bars": float(np.mean([t["bars"] for t in trades])),
           "exits": {k: sum(t["exit"] == k for t in trades) for k in ("target", "stop", "time")}}
    if len(r) >= 10:
        bs = np.random.default_rng(7).choice(r, size=(4000, len(r))).mean(1)
        out["ci_pct"] = [float(np.percentile(bs, 2.5) * 100), float(np.percentile(bs, 97.5) * 100)]
    return out


def leg_costs(S: dict, spread_bps: float, fee_pct: float) -> float:
    """Round trip on ONE leg as a fraction of its notional: fee + slippage both sides + spread."""
    return 2 * (fee_pct + S["slippage_pct"]) / 100 + spread_bps / 10000


# ── live view ──────────────────────────────────────────────────────────────
def snapshot(cfg: dict, p: dict = DEFAULT) -> dict | None:
    """Current β, z-score (last closed bar and live, using the forming bar) and recent z history."""
    pa, pb = cfg["proxy"][cfg["a"]], cfg["proxy"][cfg["b"]]
    df = aligned(pa, pb, p["tf"], limit=p["window"] * 4)
    if len(df) < p["window"] + 10:
        return None
    st = spread_stats(df, p["window"], p["hedge"])
    last = st.iloc[-1]
    out = {k: float(last[k]) for k in ("beta", "spread", "mean", "sd", "z", "corr")}
    out.update(ts=int(last["ts"]), proxy=[pa, pb], params=p, z_live=None,
               z_hist=[[int(t), round(float(z), 3)] for t, z in zip(st["ts"].iloc[-240:], st["z"].iloc[-240:]) if z == z])
    fa, fb = data.load_bars(pa, p["tf"], 1), data.load_bars(pb, p["tf"], 1)
    if len(fa) and len(fb) and not fa["closed"].iloc[-1] and not fb["closed"].iloc[-1]:
        s = np.log(fa["close"].iloc[-1]) - out["beta"] * np.log(fb["close"].iloc[-1])
        out["z_live"] = float((s - out["mean"]) / out["sd"])
    return out


def _per_day(q: dict, d: int, S: dict) -> float:
    """Funding cost per day as a fraction of notional (+ pays, − receives); Omni swaps charge rate / 360."""
    if d == 1:
        rate = q.get("funding_annual_long") or 0.0
    else:
        rate = (q.get("funding_annual_short") or 0.0) * (-1 if S.get("shorts_receive", 1) else 1)
    return rate / 360


def suggest(cfg: dict, snap: dict | None, live: dict, S: dict, reports: dict) -> dict:
    """Neutral two-leg trade on the Omni swaps when |z| ≥ z_in, sized so a stop-out loses risk_pct."""
    out = {"label": cfg["label"], "a": cfg["a"], "b": cfg["b"], "omni": cfg["omni"], "snap": snap,
           "reports": {m: {k: r.get(k) for k in ("oos", "oos_funding_today", "by_dir", "oos_from", "period", "generated")}
                       for m, r in reports.items()}}
    qa, qb = live.get(cfg["a"]) or {}, live.get(cfg["b"]) or {}
    out["marks"] = {cfg["a"]: qa.get("mark"), cfg["b"]: qb.get("mark")}
    out["funding"] = {cfg["a"]: [qa.get("funding_annual_long"), qa.get("funding_annual_short")],
                      cfg["b"]: [qb.get("funding_annual_long"), qb.get("funding_annual_short")]}
    if not snap:
        return out
    p = snap["params"]
    z = snap["z_live"] if snap["z_live"] is not None else snap["z"]
    # mode with the better out-of-sample mean; it counts as validated only if its 95% CI is above zero
    scored = [(m, (r.get("oos") or {}).get("mean_pct")) for m, r in reports.items()]
    scored = [x for x in scored if x[1] is not None]
    mode = max(scored, key=lambda x: x[1])[0] if scored else "trend"
    ci = ((reports.get(mode) or {}).get("oos") or {}).get("ci_pct")
    out.update(mode=mode, validated=bool(ci and ci[0] > 0), z=z)
    for d in (1, -1):        # carry of each orientation, per 1 000 USDC on leg A (β-hedged leg B)
        out.setdefault("carry_per_1k", {})[str(d)] = -1000 * (_per_day(qa, d, S) + snap["beta"] * _per_day(qb, -d, S))
    if abs(z) < p["z_in"] or not qa.get("mark") or not qb.get("mark"):
        out["setup"] = None
        return out
    side = 1 if z > 0 else -1
    d = side if mode == "trend" else -side          # +1: long A / short B
    stop_z, target_z = (p["z_out"], p["z_stop"]) if mode == "trend" else (p["z_stop"], p["z_out"])
    dist_stop, dist_target = abs(abs(z) - stop_z) * snap["sd"], abs(abs(z) - target_z) * snap["sd"]
    beta = snap["beta"]
    cost = leg_costs(S, qa.get("spread_bps") or cfg["spread_bps"], S[cfg["fee_key"]]) * (1 + beta)
    risk = S["capital"] * S["risk_pct"] / 100
    na = risk / (dist_stop + cost) if dist_stop + cost > 0 else 0.0
    lev = S["swap_max_lev"]
    cap = S["capital"] * S["margin_pct"] / 100
    na = min(na, cap * lev / (1 + beta))
    nb = beta * na
    carry = -(na * _per_day(qa, d, S) + nb * _per_day(qb, -d, S))
    out["setup"] = {
        "dir": d, "mode": mode, "z": z, "stop_z": side * stop_z, "target_z": side * target_z,
        "legs": [{"leg": cfg["a"], "ticker": cfg["omni"][cfg["a"]], "side": d, "mark": qa["mark"], "notional": na},
                 {"leg": cfg["b"], "ticker": cfg["omni"][cfg["b"]], "side": -d, "mark": qb["mark"], "notional": nb}],
        "lev": lev, "margin": (na + nb) / lev, "risk_usdc": risk, "cost_usdc": na * cost,
        "target_usdc": na * (dist_target - cost), "carry_usdc_day": carry,
        "oil_delta_note": f"Brent leg = β {beta:.2f} × WTI leg, so a move in oil cancels out (return correlation {snap['corr']:.2f}).",
    }
    return out
