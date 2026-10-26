"""Out-of-sample validation of the long/short probability model (backend/prob.py).
Writes research/prob_report.json and prints a summary.
Run: .venv/bin/python -W ignore research/prob_validate.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from backend import data, ict, prob, stats  # noqa: E402
from backend.config import GROUPS, SETTINGS, SYMBOLS  # noqa: E402

HTF = {"5m": "1h", "15m": "1h", "1h": "4h"}


def dataset(group: str, tf: str) -> pd.DataFrame:
    rows = []
    fee = SETTINGS[GROUPS[group]["fee_key"]]
    for sym in GROUPS[group]["symbols"]:
        b = data.load_bars(sym, tf)
        b = b[b["closed"]].reset_index(drop=True)
        if len(b) < 300:
            continue
        res = ict.run(b, tf)
        hb = data.load_bars(sym, HTF[tf])
        hb = hb[hb["closed"]].reset_index(drop=True)
        hres = ict.run(hb, HTF[tf])
        ends = np.array([data.bar_end(int(t), HTF[tf]) for t in hb["ts"]])
        when = np.array([data.bar_end(int(t), tf) for t in b["ts"]])
        idx = np.searchsorted(ends, when, side="right") - 1
        htf = np.where(idx >= 0, hres["trend"][np.clip(idx, 0, None)], 0).astype(float)
        f = prob.features(b, res, htf, tf)
        lab = prob.labels(b, res["atr"], tf)
        f = pd.concat([f, lab], axis=1)
        H = prob.HORIZON[tf]
        ts = b["ts"].to_numpy()
        f["label_end"] = ts[np.minimum(np.arange(len(ts)) + H, len(ts) - 1)]
        cost = stats.costs_pct(SETTINGS, SYMBOLS[sym]["spread_bps"], fee)
        f["cost_r"] = cost / (prob.K_ATR * f["atr"] / f["close"])
        f["symbol"] = sym
        rows.append(f.iloc[50:])                 # skip warm-up bars
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def main():
    out = {"generated": int(time.time()), "k_atr": prob.K_ATR, "horizon": prob.HORIZON, "groups": {}}
    for g in GROUPS:
        out["groups"][g] = {}
        for tf in ("5m", "15m", "1h"):
            df = dataset(g, tf)
            if df.empty:
                continue
            p, idx = prob.walk_forward(df)
            df = df.loc[idx].reset_index(drop=True)
            ev = prob.evaluate(df, p, df["cost_r"].to_numpy())
            ev55 = prob.evaluate(df, p, df["cost_r"].to_numpy(), thr=0.55)["signals"]
            per_sym = {}
            for s in df["symbol"].unique():
                m = (df["symbol"] == s).to_numpy()
                per_sym[s] = prob.evaluate(df[m].reset_index(drop=True), p[m], df["cost_r"].to_numpy()[m])["signals"]
            full = prob.fit(df.loc[df["y"].notna(), prob.FEATURES].to_numpy(float), df.loc[df["y"].notna(), "y"].to_numpy(float))
            coef = dict(zip(prob.FEATURES, np.round(full["w"][1:], 3).tolist()))
            out["groups"][g][tf] = {**ev, "signals_55": ev55, "by_symbol": per_sym, "cost_r_median": float(df["cost_r"].median()),
                                    "period": [int(df["ts"].min()), int(df["ts"].max())], "coef": coef}
            s = ev["signals"]
            print(f"{g:5} {tf:3} oos n={ev['n_oos']:5} base_up={ev['base_up']:.1f}% AUC={ev['auc']:.3f} "
                  f"logloss {ev['logloss']:.4f} vs base {ev['logloss_base']:.4f} | p≥60/≤40: n={s['n']} hit={s['hit']} "
                  f"netR={s['net_r']} CI={s.get('ci')} cov={s['coverage']:.1f}% | costR≈{df['cost_r'].median():.2f}")
            print("      calib:", " ".join(f"{b['bin']}:{b['pred']:.2f}→{b['actual']:.2f}(n{b['n']})" for b in ev["calibration"]))
    (HERE / "prob_report.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
