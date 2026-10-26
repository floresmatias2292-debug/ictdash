"""Long/short probability from the FVG-ICT indicator state.

Question answered for every closed bar:
    "From the next bar's open, does price reach +k·ATR before −k·ATR within H bars?"
Symmetric barriers, so 50% = no edge in either direction and 60% is a real edge.
P(long) = P(up barrier first), P(short) = 1 − P(long), conditional on one barrier being hit.

Model: L2-regularized logistic regression on standardized features (all from the Pine port's state at
that bar's close, plus momentum, relative volume, time of day and the higher-timeframe trend).
Walk-forward: the model is refit on everything before each test block and predicts only the block, so
every out-of-sample probability is honest. The live probability uses a fit on all labeled history.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
K_ATR = 1.0
HORIZON = {"5m": 12, "15m": 8, "1h": 8}
L2 = 3000.0           # strong shrinkage: validated AUC ≈ 0.5, so probabilities must stay near 50%

FEATURES = [
    "trend", "htf_trend", "trend_x_htf", "pd", "trend_x_pd",
    "ssl", "bsl", "bull_choch", "bear_choch", "bos_up", "bos_dn",
    "near_bull_fvg", "near_bear_fvg", "near_bull_ob", "near_bear_ob", "fvg_balance", "ob_balance",
    "new_bull_fvg", "new_bear_fvg", "or_pos", "mom3", "mom12", "rvol", "rvol_x_mom3", "t_open", "t_late",
]


def features(bars: pd.DataFrame, res: dict, htf_trend: np.ndarray, tf: str) -> pd.DataFrame:
    st = res["state"]
    c = bars["close"].to_numpy(float)
    v = bars["volume"].to_numpy(float)
    atr = np.asarray(res["atr"], float)
    n = len(c)
    dec = lambda x: np.exp(-np.asarray(x, float) / 10.0)          # recent event → 1, old/none → 0
    near = lambda x: np.nan_to_num(np.exp(-np.asarray(x, float)), nan=0.0)   # inside zone → 1, far → 0
    shift = lambda a, k: np.concatenate([np.full(k, np.nan), a[:-k]])
    with np.errstate(divide="ignore", invalid="ignore"):
        mom3 = np.clip((c - shift(c, 3)) / atr, -3, 3)
        mom12 = np.clip((c - shift(c, 12)) / atr, -5, 5)
        vma = pd.Series(v).rolling(20, min_periods=5).mean().to_numpy()
        rvol = np.clip(np.log(np.where(vma > 0, v / vma, np.nan)), -2, 2)
    mins = np.array([(lambda t: t.hour * 60 + t.minute)(datetime.fromtimestamp(int(x), ET)) for x in bars["ts"]])
    trend = res["trend"].astype(float)
    pd_ = st["pd_pos"] - 0.5
    cnt = st["count"]
    df = pd.DataFrame({
        "trend": trend, "htf_trend": htf_trend, "trend_x_htf": trend * htf_trend,
        "pd": pd_, "trend_x_pd": trend * pd_,
        "ssl": dec(st["since"]["ssl"]), "bsl": dec(st["since"]["bsl"]),
        "bull_choch": dec(st["since"]["bull_choch"]), "bear_choch": dec(st["since"]["bear_choch"]),
        "bos_up": dec(st["since"]["bos_up"]), "bos_dn": dec(st["since"]["bos_dn"]),
        "near_bull_fvg": near(st["dist"]["bull_fvg"]), "near_bear_fvg": near(st["dist"]["bear_fvg"]),
        "near_bull_ob": near(st["dist"]["bull_ob"]), "near_bear_ob": near(st["dist"]["bear_ob"]),
        "fvg_balance": (cnt["bull_fvg"] - cnt["bear_fvg"]) / 10, "ob_balance": (cnt["bull_ob"] - cnt["bear_ob"]) / 10,
        "new_bull_fvg": st["new"]["bull_fvg_score"] / 6, "new_bear_fvg": st["new"]["bear_fvg_score"] / 6,
        "or_pos": st["or_pos"], "mom3": mom3, "mom12": mom12, "rvol": rvol, "rvol_x_mom3": rvol * mom3,
        "t_open": (mins < 10 * 60 + 30).astype(float), "t_late": (mins >= 14 * 60).astype(float),
    })
    df["ts"] = bars["ts"].to_numpy()
    df["atr"] = atr
    df["close"] = c
    return df


def labels(bars: pd.DataFrame, atr: np.ndarray, tf: str) -> pd.DataFrame:
    """y = 1 up-barrier first, 0 down-barrier first, NaN neither / both in one bar / no future.
    Also the mark-to-market R at the horizon (for trading evaluation incl. timeouts)."""
    o, h, l, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n, H = len(c), HORIZON[tf]
    y = np.full(n, np.nan)
    r_long = np.full(n, np.nan)
    for i in range(n - 1):
        a = atr[i]
        if not a or np.isnan(a):
            continue
        e = o[i + 1]
        up, dn = e + K_ATR * a, e - K_ATR * a
        end = min(n, i + 1 + H)
        out = None
        for k in range(i + 1, end):
            hu, hd = h[k] >= up, l[k] <= dn
            if hu and hd:
                out = "both"
                break
            if hu:
                out = "up"
                break
            if hd:
                out = "dn"
                break
        if out == "up":
            y[i], r_long[i] = 1, K_ATR
        elif out == "dn":
            y[i], r_long[i] = 0, -K_ATR
        elif out == "both":
            r_long[i] = 0.0                       # ambiguous: excluded from the label, flat for trading
        elif end == i + 1 + H:                    # neither within H: exit at the horizon close
            r_long[i] = (c[end - 1] - e) / (K_ATR * a)
    return pd.DataFrame({"y": y, "r_long": r_long})


# ── logistic regression (IRLS / Newton with L2) ───────────────────────────────
def fit(X: np.ndarray, y: np.ndarray, l2: float | None = None) -> dict:
    l2 = L2 if l2 is None else l2
    mu, sd = np.nanmean(X, 0), np.nanstd(X, 0)
    sd[sd == 0] = 1
    Z = np.nan_to_num((X - mu) / sd)
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    w = np.zeros(Z.shape[1])
    R = l2 * np.eye(Z.shape[1])
    R[0, 0] = 0
    for _ in range(25):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + R @ w
        Hm = (Z * (p * (1 - p))[:, None]).T @ Z + R
        step = np.linalg.solve(Hm, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {"w": w, "mu": mu, "sd": sd}


def predict(m: dict, X: np.ndarray) -> np.ndarray:
    Z = np.nan_to_num((X - m["mu"]) / m["sd"])
    return 1 / (1 + np.exp(-np.clip(m["w"][0] + Z @ m["w"][1:], -30, 30)))


def walk_forward(df: pd.DataFrame, start_frac: float = 0.4, blocks: int = 6) -> np.ndarray:
    """Out-of-sample probabilities: refit on all labeled rows strictly before each block.
    A row's label needs H future bars, so training rows stop H bars (by time) before the block."""
    df = df.sort_values("ts")
    p = pd.Series(np.nan, index=df.index)
    ts = df["ts"].to_numpy()
    edges = np.quantile(ts, np.linspace(start_frac, 1, blocks + 1))
    for b in range(blocks):
        lo, hi = edges[b], edges[b + 1]
        blk = (ts > lo) & (ts <= hi) if b else (ts >= lo) & (ts <= hi)
        embargo = df["label_end"].to_numpy() < lo        # label fully known before the block starts
        tr = embargo & df["y"].notna().to_numpy()
        if tr.sum() < 200 or not blk.any():
            continue
        m = fit(df.loc[tr, FEATURES].to_numpy(float), df.loc[tr, "y"].to_numpy(float))
        p[df.index[blk]] = predict(m, df.loc[blk, FEATURES].to_numpy(float))
    return p.reindex(df.index).to_numpy(), df.index


def evaluate(df: pd.DataFrame, p: np.ndarray, cost_r: np.ndarray, thr: float = 0.60) -> dict:
    """Out-of-sample quality of the probabilities and of trading them."""
    ok = ~np.isnan(p)
    lab = ok & ~np.isnan(df["y"].to_numpy())
    y, pp = df["y"].to_numpy()[lab], p[lab]
    base = y.mean() if len(y) else np.nan
    eps = 1e-9
    ll = -np.mean(y * np.log(pp + eps) + (1 - y) * np.log(1 - pp + eps)) if len(y) else np.nan
    ll0 = -np.mean(y * np.log(base + eps) + (1 - y) * np.log(1 - base + eps)) if len(y) else np.nan
    order = np.argsort(pp)
    ranks = np.empty(len(pp)); ranks[order] = np.arange(len(pp))
    npos = y.sum()
    auc = (ranks[y == 1].sum() - npos * (npos - 1) / 2) / (npos * (len(y) - npos)) if 0 < npos < len(y) else np.nan
    bins = []
    for lo_, hi_ in ((0, .35), (.35, .4), (.4, .45), (.45, .5), (.5, .55), (.55, .6), (.6, .65), (.65, 1.01)):
        m = (pp >= lo_) & (pp < hi_)
        if m.sum():
            bins.append({"bin": f"{lo_:.2f}-{min(hi_, 1):.2f}", "n": int(m.sum()), "pred": float(pp[m].mean()),
                         "actual": float(y[m].mean())})
    # trading: long when p ≥ thr, short when p ≤ 1 − thr, 1:1 ATR barriers, timeouts at market
    r = df["r_long"].to_numpy()
    longs = ok & (p >= thr) & ~np.isnan(r)
    shorts = ok & (p <= 1 - thr) & ~np.isnan(r)
    rr = np.concatenate([r[longs] - cost_r[longs], -r[shorts] - cost_r[shorts]])
    hit = np.concatenate([(r[longs] > 0), (r[shorts] < 0)])
    sig = {"n": int(len(rr)), "n_long": int(longs.sum()), "n_short": int(shorts.sum()),
           "hit": float(hit.mean() * 100) if len(rr) else None, "net_r": float(rr.mean()) if len(rr) else None,
           "coverage": float((longs.sum() + shorts.sum()) / max(ok.sum(), 1) * 100)}
    if len(rr) >= 10:
        rng = np.random.default_rng(7)
        bs = rng.choice(rr, size=(2000, len(rr))).mean(1)
        sig["ci"] = (float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)))
    return {"n_oos": int(lab.sum()), "base_up": float(base * 100) if len(y) else None,
            "logloss": float(ll), "logloss_base": float(ll0), "auc": float(auc) if auc == auc else None,
            "calibration": bins, "signals": sig}
