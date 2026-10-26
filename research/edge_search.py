"""Walk-forward test of each feature family for the long/short probability (label: ±1×ATR barrier).

For every group × timeframe × family: walk-forward logistic fit (refit before each block, embargo on label
overlap), out-of-sample AUC with a day-block bootstrap 95% interval (days resampled, not bars, because
neighbouring bars are correlated), log-loss vs the base rate, AUC per walk-forward block and per symbol.
Writes research/edge_report.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import edge_features as ef  # noqa: E402
import prob_validate as pv  # noqa: E402
from backend import data, prob  # noqa: E402
from backend.config import GROUPS  # noqa: E402

L2_GRID = (30.0, 300.0)


def build(group: str, tf: str) -> pd.DataFrame:
    base = pv.dataset(group, tf)
    extra = []
    for sym in GROUPS[group]["symbols"]:
        b = data.load_bars(sym, tf)
        b = b[b["closed"]].reset_index(drop=True)
        if len(b) < 300:
            continue
        atr = base.loc[base["symbol"] == sym].set_index("ts")["atr"]
        full_atr = b["ts"].map(atr).to_numpy(float)
        f = ef.per_symbol(b, full_atr)
        f["symbol"] = sym
        extra.append(f)
    ex = pd.concat(extra, ignore_index=True)
    df = base.merge(ex, on=["symbol", "ts"], how="left")
    return ef.add_cross(df)


def auc(y, p):
    order = np.argsort(p); r = np.empty(len(p)); r[order] = np.arange(len(p))
    n1 = y.sum(); n0 = len(y) - n1
    return (r[y == 1].sum() - n1 * (n1 - 1) / 2) / (n1 * n0) if n1 and n0 else np.nan


def evaluate(df: pd.DataFrame, feats: list[str], l2: float) -> dict:
    old_f, old_l2 = prob.FEATURES, prob.L2
    prob.FEATURES, prob.L2 = feats, l2
    try:
        p, idx = prob.walk_forward(df)
    finally:
        prob.FEATURES, prob.L2 = old_f, old_l2
    d = df.loc[idx].assign(p=p)
    d = d[d["p"].notna() & d["y"].notna()]
    y, pp = d["y"].to_numpy(), d["p"].to_numpy()
    base = y.mean()
    ll = -np.mean(y * np.log(pp) + (1 - y) * np.log(1 - pp))
    ll0 = -np.mean(y * np.log(base) + (1 - y) * np.log(1 - base))
    days = pd.to_datetime(d["ts"], unit="s").dt.date.to_numpy()
    ud = np.unique(days)
    rng = np.random.default_rng(11)
    groups = {u: np.where(days == u)[0] for u in ud}
    boots = []
    for _ in range(300):
        pick = np.concatenate([groups[u] for u in rng.choice(ud, len(ud))])
        boots.append(auc(y[pick], pp[pick]))
    blocks = np.array_split(np.argsort(d["ts"].to_numpy()), 4)
    return {"n": int(len(d)), "auc": float(auc(y, pp)), "ci": [float(np.nanpercentile(boots, 2.5)), float(np.nanpercentile(boots, 97.5))],
            "dll": float(ll - ll0), "blocks": [float(auc(y[b], pp[b])) for b in blocks],
            "by_symbol": {s: float(auc(y[(d["symbol"] == s).to_numpy()], pp[(d["symbol"] == s).to_numpy()])) for s in d["symbol"].unique()}}


def main():
    out = {"generated": int(time.time()), "families": ef.FAMILIES, "results": {}}
    sets = {**{k: v for k, v in ef.FAMILIES.items()}, "ict_state": prob.FEATURES,
            "all_new": sum(ef.FAMILIES.values(), []), "everything": prob.FEATURES + sum(ef.FAMILIES.values(), [])}
    for g in GROUPS:
        for tf in ("5m", "15m", "1h"):
            df = build(g, tf)
            key = f"{g}/{tf}"
            out["results"][key] = {}
            for name, feats in sets.items():
                best = None
                for l2 in L2_GRID:
                    r = evaluate(df, feats, l2)
                    r["l2"] = l2
                    out["results"][key].setdefault(name, []).append(r)
                    best = r if best is None or r["auc"] > best["auc"] else best
                flag = "  <-- CI>0.5" if any(x["ci"][0] > 0.5 for x in out["results"][key][name]) else ""
                print(f"{key:10} {name:10} " + " | ".join(f"L2={x['l2']:.0f} AUC {x['auc']:.3f} [{x['ci'][0]:.3f},{x['ci'][1]:.3f}] Δll {x['dll']:+.4f} blocks {' '.join(f'{b:.2f}' for b in x['blocks'])}"
                                                         for x in out["results"][key][name]) + flag)
            (HERE / "edge_report.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
