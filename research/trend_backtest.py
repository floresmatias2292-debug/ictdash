"""Historical test of the dashboard's TREND mode (portfolio.suggest), using Yahoo bars for the live move.

Two versions:
  proxy1h  decision on every closed 1h bar (≈3 years). Structure from 1h + 4h (5m history is only 60 days),
           live move = the last 1h bar (close − open) in units of 2 × ATR5m, with ATR5m ≈ ATR1h / √12.
  exact5m  decision on every closed 5m bar (≈60 days). Same formula as the dashboard: structure 5m/1h/4h,
           live = 0.6 × clip(60-min move / 2·ATR5m) + 0.4 × clip(15-min move / ATR5m).
Strength = 0.7 × structure + 0.3 × live (NY session open, as in the dashboard).
TREND when |mean sector strength| ≥ threshold and every sector symbol has the same sign; trade the most
extreme symbol (ties within 0.05 → larger unclipped move in ATRs).
Trade: enter at the next bar's open, stop 1.5 × ATR(1h), target 2R, exit at market after 8 hours.
One position per group at a time. Net R = gross R − round-trip costs in R (fee + slippage + spread).
Controls: structure only (w=0), live only (w=1), and the same entries traded in the opposite direction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import data, ict, stats  # noqa: E402
from backend.config import GROUPS, ICT_DEFAULTS, SETTINGS, SYMBOLS  # noqa: E402

PD_LEN = ICT_DEFAULTS["pd_len"]
HOLD_HOURS = 8          # wall-clock time stop (dashboard has none; tested 8 h … 5 days)


def _prep(sym: str) -> dict:
    out = {}
    for tf in ("5m", "1h", "4h"):
        b = data.load_bars(sym, tf)
        b = b[b["closed"]].reset_index(drop=True)
        r = ict.run(b, tf)
        hi = b["high"].rolling(PD_LEN).max().to_numpy()
        lo = b["low"].rolling(PD_LEN).min().to_numpy()
        c = b["close"].to_numpy()
        pos = np.where(hi - lo > 0, (c - lo) / (hi - lo) * 100, 50.0)
        zone = np.where(pos > 55, -1, np.where(pos < 45, 1, 0))       # -1 premium, +1 discount, 0 eq
        trend = r["trend"]
        pd_b = np.where((zone == 1) & (trend == 1), 0.5, np.where((zone == -1) & (trend == -1), -0.5, 0.0))
        ends = np.array([data.bar_end(int(t), tf) for t in b["ts"]])
        out[tf] = {"bars": b, "x": (trend + pd_b) / 2.5, "ends": ends, "atr": r["atr"]}
    return out


def _asof(p: dict, tf: str, when: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(p[tf]["ends"], when, side="right") - 1
    v = np.full(len(when), np.nan)
    ok = idx >= 0
    v[ok] = p[tf]["x"][idx[ok]]
    return v


def simulate(group: str, version: str, thr: float = 0.35, w_live: float = 0.3, flip: bool = False,
             S: dict | None = None) -> pd.DataFrame:
    S = S or SETTINGS
    g = GROUPS[group]
    syms = g["sector"]
    P = {s: _prep(s) for s in syms}
    dec_tf = "1h" if version == "proxy1h" else "5m"
    # decision grid: bars present for every sector symbol
    common = None
    for s in syms:
        t = set(P[s][dec_tf]["bars"]["ts"].tolist())
        common = t if common is None else common & t
    grid = np.array(sorted(common))
    if version == "exact5m":   # need 4h/1h history before the 5m window starts
        grid = grid[grid >= max(P[s]["1h"]["bars"]["ts"].iloc[0] for s in syms)]
    when = np.array([data.bar_end(int(t), dec_tf) for t in grid])
    strength, z = {}, {}
    for s in syms:
        p = P[s]
        b = p[dec_tf]["bars"].set_index("ts").loc[grid]
        atr1h = pd.Series(p["1h"]["atr"]).to_numpy()
        idx1h = np.searchsorted(p["1h"]["ends"], when, side="right") - 1
        a1h = np.where(idx1h >= 0, atr1h[np.clip(idx1h, 0, None)], np.nan)
        if version == "proxy1h":
            struct = (2 * _asof(p, "1h", when) + 3 * _asof(p, "4h", when)) / 5
            atr5 = a1h / np.sqrt(12)
            m60 = (b["close"] - b["open"]).to_numpy()
            live = np.clip(m60 / (2 * atr5), -1, 1)
        else:
            struct = (1 * _asof(p, "5m", when) + 2 * _asof(p, "1h", when) + 3 * _asof(p, "4h", when)) / 6
            full = p["5m"]["bars"].set_index("ts")
            pos = full.index.get_indexer(grid)
            o = full["open"].to_numpy()
            c = full["close"].to_numpy()
            atr5 = pd.Series(p["5m"]["atr"]).to_numpy()[pos]
            m60 = c[pos] - o[np.clip(pos - 11, 0, None)]
            m15 = c[pos] - o[np.clip(pos - 2, 0, None)]
            live = 0.6 * np.clip(m60 / (2 * atr5), -1, 1) + 0.4 * np.clip(m15 / atr5, -1, 1)
        strength[s] = (1 - w_live) * struct + w_live * live
        z[s] = m60 / atr5
        P[s]["_a1h"] = a1h

    trades = []
    busy_until = -1
    M = np.vstack([strength[s] for s in syms])                       # (n_syms, n_grid)
    Z = np.vstack([z[s] for s in syms])
    for k in range(len(grid) - 1):
        if grid[k] <= busy_until or np.isnan(M[:, k]).any():
            continue
        sector = M[:, k].mean()
        if abs(sector) < thr or len(set(np.sign(M[:, k]))) != 1:
            continue
        d = 1 if sector > 0 else -1
        top = (d * M[:, k]).max()
        cand = [j for j in range(len(syms)) if d * M[j, k] >= top - 0.05]
        j = max(cand, key=lambda j: d * Z[j, k])
        sym = syms[j]
        if flip:
            d = -d
        res = _trade(P[sym], dec_tf, grid[k], d, S, group, sym)
        if res:
            res.update(symbol=sym, sector=float(sector), strength=float(M[j, k]))
            trades.append(res)
            busy_until = res["exit_ts"]
    return pd.DataFrame(trades)


def _trade(p: dict, tf: str, signal_ts: int, d: int, S: dict, group: str, sym: str) -> dict | None:
    b = p[tf]["bars"]
    ts = b["ts"].to_numpy()
    i = np.searchsorted(ts, signal_ts, side="right")              # next bar
    if i >= len(ts):
        return None
    o, h, l, c = (b[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx1h = np.searchsorted(p["1h"]["ends"], data.bar_end(int(signal_ts), tf), side="right") - 1
    atr = p["1h"]["atr"][idx1h] if idx1h >= 0 else np.nan
    if not np.isfinite(atr) or atr <= 0:
        return None
    entry = o[i]
    R = 1.5 * atr
    stop, target = entry - d * R, entry + d * S["rr"] * R
    limit_ts = ts[i] + HOLD_HOURS * 3600
    exit_px, exit_ts, status = None, None, None
    k = i
    while k < len(ts) and ts[k] < limit_ts:
        if (l[k] <= stop) if d == 1 else (h[k] >= stop):
            exit_px = stop if (o[k] - stop) * d > 0 or k == i else o[k]
            status = "loss"
            break
        if ((h[k] >= target) if d == 1 else (l[k] <= target)) and k > i:
            exit_px = target if (target - o[k]) * d > 0 else o[k]
            status = "win"
            break
        k += 1
    if status is None:
        if k >= len(ts):
            return None                                             # still open at the end of data
        exit_px, status = o[k], "timeout"                           # first bar after 8 h: exit at its open
    exit_ts = int(ts[k])
    fee = S[GROUPS[group]["fee_key"]]
    cost = stats.costs_pct(S, SYMBOLS[sym]["spread_bps"], fee)
    stop_pct = R / entry
    r = (exit_px - entry) * d / R
    return {"entry_ts": int(ts[i]), "exit_ts": exit_ts, "dir": d, "entry": entry, "exit": exit_px, "status": status,
            "r": r, "cost_r": cost / stop_pct, "net_r": r - cost / stop_pct, "win": int(status == "win")}


def summary(df: pd.DataFrame) -> dict:
    if df is None or not len(df):
        return {"n": 0}
    x = df["net_r"].clip(-3, 3).to_numpy()
    rng = np.random.default_rng(7)
    m = rng.choice(x, size=(2000, len(x)), replace=True).mean(axis=1) if len(x) >= 5 else np.array([np.nan])
    return {"n": len(df), "win": df["win"].mean() * 100, "net": float(x.mean()),
            "ci": (float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))), "cost": float(df["cost_r"].median())}
