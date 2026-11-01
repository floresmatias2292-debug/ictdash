"""Does US100 (with US30) lead the market, and can that be traded on SOXL and on an AMD / NVDA pair?

Data: the Yahoo cache (data/bars.sqlite), US cash session, all five markets on the same bars:
1h since Nov 2023, 5m since Jul 2026. Weekend tests add the 24/7 Hyperliquid US100 perp (data/market.sqlite).

Tests (all walk-forward: settings chosen only on data before each test block, net of costs)
  1. Big move ahead: P(|move over the next H bars| ≥ 1.5 × ATR) from compression, time of day, Monday,
     relative volume and the US100 / US30 divergence. Direction: P(up) from US100 + US30 momentum.
  2. Weekend: does the Monday gap continue or fade during Monday? Does the perp's weekend move predict the gap?
  3. SOXL scalping: when US100's last-k-bar move is strong (z ≥ threshold), trade SOXL the same way for H bars.
     Control: the same rule driven by SOXL's own move.
  4. AMD / NVDA pair: equal notional (x10, 15 USDC each). US100 bullish → long the higher-beta stock, short the
     lower-beta one; bearish → the reverse.
Writes research/us100_lead_report.json.   Run: .venv/bin/python -W ignore research/us100_lead.py
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from backend import lead, prob  # noqa: E402
from backend.config import SETTINGS  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = HERE.parent / "data" / "bars.sqlite"
LIVE = HERE.parent / "data" / "market.sqlite"
SYMS = ["US100", "US30", "SOXL", "AMD", "NVDA"]
BLOCKS, START, MIN_TRADES = 6, 0.4, 15
# round trip on one leg, fraction of notional: fee + slippage both sides + spread (Omni perps ≈ 5 bp)
LEG_COST = 2 * (SETTINGS["fee_pct"] + SETTINGS["slippage_pct"]) / 100 + 5.0 / 10000


def panel(tf: str) -> pd.DataFrame:
    """Wide frame: ts + open/high/low/close/volume per symbol, only bars where all five exist."""
    with sqlite3.connect(CACHE) as con:
        df = pd.read_sql_query("SELECT symbol, ts, open, high, low, close, volume FROM bars WHERE tf=? AND symbol IN "
                               f"({','.join('?' * len(SYMS))})", con, params=(tf, *SYMS))
    w = df.pivot(index="ts", columns="symbol")
    w.columns = [f"{c}_{s}" for c, s in w.columns]
    w = w.dropna().reset_index().sort_values("ts").reset_index(drop=True)
    et = [datetime.fromtimestamp(int(t), ET) for t in w["ts"]]
    w["day"] = [t.date().toordinal() for t in et]
    w["mins"] = [t.hour * 60 + t.minute - 570 for t in et]          # minutes since 09:30 ET
    w["wd"] = [t.weekday() for t in et]
    return w


def atr(w, s, n=14):
    h, l, c = w[f"high_{s}"], w[f"low_{s}"], w[f"close_{s}"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def mom_z(w, s, k, win=200):
    """k-bar log return of s divided by its rolling standard deviation (known at the bar's close)."""
    r = np.log(w[f"close_{s}"]).diff(k)
    return r / r.rolling(win, min_periods=50).std()


# ── walk-forward helpers ────────────────────────────────────────────────────
def edges(ts):
    return np.quantile(ts, np.linspace(START, 1, BLOCKS + 1))


def tstat(tr):
    if len(tr) < MIN_TRADES:
        return -np.inf
    r = np.array([t["net"] for t in tr])
    return r.mean() / (r.std(ddof=1) / np.sqrt(len(r)) + 1e-12)


def walk(sims: dict, ts) -> tuple[list, list]:
    """sims: key -> trades. Pick the best key on trades closed before each block, keep its block trades."""
    oos, picks = [], []
    e = edges(ts)
    for k in range(BLOCKS):
        lo, hi = e[k], e[k + 1]
        best = max(sims, key=lambda key: tstat([t for t in sims[key] if t["exit_ts"] < lo]))
        blk = [t for t in sims[best] if lo <= t["entry_ts"] < hi]
        oos += blk
        picks.append({"block": k, "key": best, "n": len(blk)})
    return oos, picks


def summ(tr):
    if not tr:
        return {"n": 0}
    r = np.array([t["net"] for t in tr]) * 100
    out = {"n": len(r), "hit": float((r > 0).mean() * 100), "mean_pct": float(r.mean()), "sum_pct": float(r.sum())}
    if len(r) >= 10:
        bs = np.random.default_rng(7).choice(r, size=(4000, len(r))).mean(1)
        out["ci_pct"] = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
    return out


def logistic_wf(X: pd.DataFrame, y: pd.Series, ts: np.ndarray, H: int) -> dict:
    """Walk-forward logistic regression (prob.fit with mild L2) → out-of-sample AUC and lift."""
    ok = y.notna().to_numpy() & np.isfinite(X.to_numpy()).all(1)
    X, y, ts = X[ok].to_numpy(float), y[ok].to_numpy(float), ts[ok]
    p = np.full(len(y), np.nan)
    e = edges(ts)
    for k in range(BLOCKS):
        lo, hi = e[k], e[k + 1]
        tr = np.arange(len(ts)) < np.searchsorted(ts, lo) - H          # embargo: labels known before the block
        te = (ts >= lo) & (ts < hi) if k < BLOCKS - 1 else (ts >= lo)
        if tr.sum() < 200:
            continue
        m = prob.fit(X[tr], y[tr], l2=10.0)
        p[te] = prob.predict(m, X[te])
    m = ~np.isnan(p)
    yy, pp = y[m], p[m]
    order = np.argsort(pp)
    ranks = np.empty(len(pp)); ranks[order] = np.arange(len(pp))
    npos = yy.sum()
    auc = (ranks[yy == 1].sum() - npos * (npos - 1) / 2) / (npos * (len(yy) - npos))
    top = pp >= np.quantile(pp, 0.8)
    return {"n_oos": int(m.sum()), "auc": float(auc), "base": float(yy.mean() * 100),
            "top20_rate": float(yy[top].mean() * 100), "bottom20_rate": float(yy[pp <= np.quantile(pp, 0.2)].mean() * 100)}


# ── 1. big move ahead + direction ───────────────────────────────────────────
def test_big_move(w, tf, H, k_atr=1.5):
    # exactly the live model's features (backend/lead.py), US100 only
    b = pd.DataFrame({"ts": w["ts"], "high": w["high_US100"], "low": w["low_US100"], "close": w["close_US100"],
                      "volume": w["volume_US100"]})
    X, a, _ = lead.features(b)
    big = lead.big_label(b, a, H, k_atr)
    c = w["close_US100"]
    fwd = c.shift(-H) - c
    div = (mom_z(w, "US100", 6) - mom_z(w, "US30", 6)).clip(-4, 4)
    big_res = logistic_wf(X[lead.FEATS], big, w["ts"].to_numpy(), H)
    big_res["with_us30_divergence"] = logistic_wf(X[lead.FEATS].assign(abs_div=div.abs()), big, w["ts"].to_numpy(), H)["auc"]
    up = (fwd > 0).astype(float).where(fwd.notna())
    Xd = pd.DataFrame({"mom3": mom_z(w, "US100", 3).clip(-4, 4), "mom12": mom_z(w, "US100", 12).clip(-4, 4),
                       "us30_mom3": mom_z(w, "US30", 3).clip(-4, 4), "agree": np.sign(mom_z(w, "US100", 6)) * np.sign(mom_z(w, "US30", 6)),
                       "div": div})
    dir_res = logistic_wf(Xd, up, w["ts"].to_numpy(), H)
    return {"tf": tf, "H": H, "k_atr": k_atr, "big_move": big_res, "direction": dir_res}


# ── 2. weekend gap ──────────────────────────────────────────────────────────
def test_weekend(w1h):
    g = w1h.groupby("day")
    days = pd.DataFrame({"open": g["open_US100"].first(), "close": g["close_US100"].last(),
                         "soxl_open": g["open_SOXL"].first(), "soxl_close": g["close_SOXL"].last(),
                         "wd": g["wd"].first(), "ts0": g["ts"].first()})
    days["prev_close"], days["prev_soxl"] = days["close"].shift(), days["soxl_close"].shift()
    mon = days[(days["wd"] == 0) & days["prev_close"].notna()].copy()
    mon["gap"] = mon["open"] / mon["prev_close"] - 1
    mon["day_ret"] = mon["close"] / mon["open"] - 1
    mon["soxl_gap"] = mon["soxl_open"] / mon["prev_soxl"] - 1
    mon["soxl_day"] = mon["soxl_close"] / mon["soxl_open"] - 1
    big = mon[mon["gap"].abs() >= mon["gap"].abs().median()]
    cont = lambda d, col: float((np.sign(d["gap"]) == np.sign(d[col])).mean() * 100)
    out = {"mondays": int(len(mon)), "gap_abs_median_pct": float(mon["gap"].abs().median() * 100),
           "us100_continue_pct": cont(mon, "day_ret"), "us100_continue_pct_big_gaps": cont(big, "day_ret"),
           "soxl_follows_us100_gap_pct": cont(mon, "soxl_day"),
           "corr_gap_vs_monday": float(np.corrcoef(mon["gap"], mon["day_ret"])[0, 1])}
    # perp weekend move (Friday cash close → Monday 09:00 ET on the 24/7 Hyperliquid perp) vs the cash gap
    with sqlite3.connect(LIVE) as con:
        perp = pd.read_sql_query("SELECT ts, close FROM bars WHERE symbol='US100' AND tf='1h' ORDER BY ts", con)
    perp = perp.set_index("ts")["close"]
    rows = []
    for _, m in mon.iterrows():
        t_open = int(m["ts0"])                         # Monday first cash bar (09:30 ET)
        fri = days[(days["ts0"] < t_open)].iloc[-1:]   # previous session (usually Friday)
        if fri.empty:
            continue
        t_fri_close = int(fri["ts0"].iloc[0]) + 7 * 3600     # ≈ 16:00 ET
        a, b = perp.loc[:t_fri_close], perp.loc[:t_open - 1800]
        if len(a) == 0 or len(b) == 0 or a.index[-1] < t_fri_close - 7200 or b.index[-1] < t_open - 5400:
            continue
        rows.append((b.iloc[-1] / a.iloc[-1] - 1, m["gap"]))
    # trade: at Monday's cash open, bet against the US100 gap until the close (US100 swap and SOXL perp)
    swap_cost = 2 * (SETTINGS["swap_fee_pct"] + SETTINGS["slippage_pct"]) / 100 + 0.4 / 10000
    mon["fade_us100"] = -np.sign(mon["gap"]) * mon["day_ret"] - swap_cost
    mon["fade_soxl"] = -np.sign(mon["gap"]) * mon["soxl_day"] - LEG_COST
    half = len(mon) // 2
    for col in ("fade_us100", "fade_soxl"):
        tr = [{"net": v} for v in mon[col]]
        out[col] = {"all": summ(tr), "first_half": summ(tr[:half]), "second_half": summ(tr[half:]),
                    "big_gaps": summ([{"net": v} for v in big.index.map(mon[col])])}
    if len(rows) >= 8:
        r = np.array(rows)
        out["perp_weekend"] = {"n": len(r), "corr_perp_move_vs_gap": float(np.corrcoef(r[:, 0], r[:, 1])[0, 1]),
                               "same_sign_pct": float((np.sign(r[:, 0]) == np.sign(r[:, 1])).mean() * 100)}
    # weekend hours: does the Binance SOXL perp lag the Hyperliquid US100 perp?
    with sqlite3.connect(LIVE) as con:
        q = lambda sym: pd.read_sql_query("SELECT ts, close FROM bars WHERE symbol=? AND tf='1h'", con, params=(sym,)).set_index("ts")["close"]
        both = pd.concat({"us": q("US100"), "sx": q("SOXL")}, axis=1).dropna().sort_index()
    wk = np.array([datetime.fromtimestamp(int(t), ET).weekday() >= 5 for t in both.index])
    r = np.log(both).diff()
    nxt = r["sx"].shift(-1)
    out["weekend_perps"] = {"hours": int(wk.sum()), "corr_same_hour": float(r["us"][wk].corr(r["sx"][wk])),
                            "corr_us100_then_soxl_next_hour": float(r["us"][wk].corr(nxt[wk])),
                            "weekday_corr_same_hour": float(r["us"][~wk].corr(r["sx"][~wk]))}
    return out


# ── 3. SOXL scalping driven by US100 ────────────────────────────────────────
def follow_trades(w, driver, target, k, thr, H, cost):
    z = mom_z(w, driver, k).to_numpy()
    o, c, ts, day = (w[f"open_{target}"].to_numpy(), w[f"close_{target}"].to_numpy(), w["ts"].to_numpy(),
                     w["day"].to_numpy())
    tr, i, n = [], 0, len(w)
    while i < n - H - 1:
        if np.isnan(z[i]) or abs(z[i]) < thr or day[i + H] != day[i + 1]:     # no trade held through the close
            i += 1
            continue
        d = 1 if z[i] > 0 else -1
        e, x = i + 1, i + H
        g = d * (c[x] / o[e] - 1)
        tr.append({"entry_ts": int(ts[e]), "exit_ts": int(ts[x]), "dir": d, "net": g - cost})
        i = x + 1
    return tr


def test_soxl(w, tf):
    grid = {"k": [3, 6, 12], "thr": [1.0, 1.5, 2.0], "H": [3, 6, 12]}
    out = {"tf": tf}
    for drv in ("US100", "SOXL"):
        sims = {(k, t, h): follow_trades(w, drv, "SOXL", k, t, h, LEG_COST)
                for k, t, h in itertools.product(*grid.values())}
        oos, picks = walk(sims, w["ts"].to_numpy())
        out[f"driver_{drv}"] = {"oos": summ(oos), "picks": [p["key"] for p in picks]}
        out[f"driver_{drv}"]["gross_oos"] = summ([{**t, "net": t["net"] + LEG_COST} for t in oos])
    # contemporaneous vs lead: correlation of SOXL's next-bar return with US100's last-bar return
    r_us, r_sx = np.log(w["close_US100"]).diff(), np.log(w["close_SOXL"]).diff()
    same = w["day"].diff() == 0
    out["corr_same_bar"] = float(r_us[same].corr(r_sx[same]))
    out["corr_us100_then_soxl_next"] = float(r_us[same].corr(r_sx.shift(-1)[same]))
    out["beta_soxl_vs_us100"] = float(r_sx[same].cov(r_us[same]) / r_us[same].var())
    return out


# ── 4. AMD / NVDA pair by US100 regime ──────────────────────────────────────
def pair_trades(w, k, thr, H, win, cost):
    z = mom_z(w, "US100", k).to_numpy()
    r = {s: np.log(w[f"close_{s}"]).diff() for s in ("US100", "AMD", "NVDA")}
    beta = {s: (r[s].rolling(win, min_periods=win // 2).cov(r["US100"]) / r["US100"].rolling(win, min_periods=win // 2).var()).to_numpy()
            for s in ("AMD", "NVDA")}
    o = {s: w[f"open_{s}"].to_numpy() for s in ("AMD", "NVDA")}
    c = {s: w[f"close_{s}"].to_numpy() for s in ("AMD", "NVDA")}
    ts, day = w["ts"].to_numpy(), w["day"].to_numpy()
    tr, i, n = [], 0, len(w)
    while i < n - H - 1:
        if np.isnan(z[i]) or abs(z[i]) < thr or np.isnan(beta["AMD"][i]) or day[i + H] != day[i + 1]:
            i += 1
            continue
        hi_b = "NVDA" if beta["NVDA"][i] >= beta["AMD"][i] else "AMD"
        lo_b = "AMD" if hi_b == "NVDA" else "NVDA"
        lng, sht = (hi_b, lo_b) if z[i] > 0 else (lo_b, hi_b)
        e, x = i + 1, i + H
        g = (c[lng][x] / o[lng][e] - 1) - (c[sht][x] / o[sht][e] - 1)     # equal notional on both legs
        tr.append({"entry_ts": int(ts[e]), "exit_ts": int(ts[x]), "long": lng, "net": (g - 2 * cost) / 2})
        i = x + 1
    return tr


def test_pair(w, tf):
    grid = {"k": [3, 6, 12], "thr": [1.0, 1.5, 2.0], "H": [3, 6, 12], "win": [100, 300]}
    sims = {key: pair_trades(w, *key, LEG_COST) for key in itertools.product(*grid.values())}
    oos, picks = walk(sims, w["ts"].to_numpy())
    r = {s: np.log(w[f"close_{s}"]).diff() for s in ("US100", "AMD", "NVDA")}
    same = w["day"].diff() == 0
    return {"tf": tf, "oos": summ(oos), "gross_oos": summ([{**t, "net": t["net"] + LEG_COST} for t in oos]),
            "picks": [p["key"] for p in picks],
            "beta_full": {s: float(r[s][same].cov(r["US100"][same]) / r["US100"][same].var()) for s in ("AMD", "NVDA")},
            "note": "net is per unit of total notional (both legs); costs = one round trip on each leg"}


def main():
    rep = {"generated": int(time.time()), "leg_cost_pct": LEG_COST * 100, "tests": {}}
    for tf, H, k_atr in (("1h", 3, 1.5), ("5m", 12, 3.0)):
        w = panel(tf)
        print(f"\n=== {tf}: {len(w)} aligned bars {datetime.fromtimestamp(w.ts.iloc[0]):%Y-%m-%d} → {datetime.fromtimestamp(w.ts.iloc[-1]):%Y-%m-%d}")
        t1 = test_big_move(w, tf, H, k_atr)
        print(" big move  :", t1["big_move"])
        print(" direction :", t1["direction"])
        t3 = test_soxl(w, tf)
        print(" SOXL by US100:", t3["driver_US100"]["oos"], "| gross", t3["driver_US100"]["gross_oos"].get("mean_pct"))
        print(" SOXL by SOXL :", t3["driver_SOXL"]["oos"], "| gross", t3["driver_SOXL"]["gross_oos"].get("mean_pct"))
        print(f" corr same bar {t3['corr_same_bar']:.3f} · US100 bar → SOXL next bar {t3['corr_us100_then_soxl_next']:.3f} · beta {t3['beta_soxl_vs_us100']:.2f}")
        t4 = test_pair(w, tf)
        print(" AMD/NVDA pair:", t4["oos"], "| gross", t4["gross_oos"].get("mean_pct"), "| betas", t4["beta_full"])
        rep["tests"][tf] = {"big_move_direction": t1, "soxl": t3, "pair": t4}
    wk = test_weekend(panel("1h"))
    print("\n=== weekend:", wk)
    rep["weekend"] = wk
    (HERE / "us100_lead_report.json").write_text(json.dumps(rep, indent=1, default=float))


if __name__ == "__main__":
    main()
