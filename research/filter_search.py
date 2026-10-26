"""Out-of-sample filter search.

Per group and timeframe the setups are split by time: the oldest 60% (train) choose filters,
the newest 40% (test) is looked at once to validate them.

Candidates: single conditions and pairs of them (cost_ok always allowed as the second leg).
Selection on train only: n ≥ MIN_N, mean net R > 0 and above the unfiltered baseline,
ranked by mean net R (clipped to [−3, +3] so a few gap trades can't drive the ranking).
Test report: n, win %, mean net R with a 95% bootstrap interval, and per-symbol mean net R.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
MIN_N = {"5m": 30, "1h": 60, "4h": 40}
COST_OK = 0.25

CONDS = {
    "align_own": lambda d: d.align_own == 1,
    "counter_own": lambda d: d.align_own == 0,
    "align_4h": lambda d: d.align_4h == 1,
    "counter_4h": lambda d: d.counter_4h == 1,
    "align_1h": lambda d: d.align_1h == 1,
    "open": lambda d: d.session == "open",
    "mid": lambda d: d.session == "mid",
    "late": lambda d: d.session == "late",
    "score<=2": lambda d: d.score <= 2,
    "score>=3": lambda d: d.score >= 3,
    "small_fvg": lambda d: d.size_atr < d.size_atr.median(),
    "large_fvg": lambda d: d.size_atr >= d.size_atr.median(),
    "long": lambda d: d.dir == 1,
    "short": lambda d: d.dir == -1,
    "cost_ok": lambda d: d.cost_r <= COST_OK,
    **{f"{c}": (lambda c: lambda d: d[f"c_{c}"])(c) for c in ("strong", "pd", "sweep", "mss", "ob")},
    **{f"no_{c}": (lambda c: lambda d: ~d[f"c_{c}"])(c) for c in ("strong", "pd", "sweep", "mss", "ob")},
}


def stats_of(d: pd.DataFrame) -> dict:
    if len(d) == 0:
        return {"n": 0, "win": np.nan, "net": np.nan, "net_clip": np.nan}
    return {"n": len(d), "win": d.win.mean() * 100, "net": d.net_r.mean(), "net_clip": d.net_r.clip(-3, 3).mean()}


def boot_ci(x: np.ndarray, n=2000, seed=7) -> tuple[float, float]:
    if len(x) < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def mask(d, names):
    m = np.ones(len(d), dtype=bool)
    for nm in names:
        m &= CONDS[nm](d).to_numpy()
    return m


def run(df: pd.DataFrame, top_k: int = 3) -> list[dict]:
    out = []
    names = list(CONDS)
    combos = [(a,) for a in names] + list(itertools.combinations(names, 2))
    for (grp, tf), d in df.groupby(["group", "tf"]):
        d = d.sort_values("born_ts").reset_index(drop=True)
        cut = d.born_ts.quantile(0.6)
        tr, te = d[d.born_ts <= cut].reset_index(drop=True), d[d.born_ts > cut].reset_index(drop=True)
        base_tr, base_te = stats_of(tr), stats_of(te)
        cands = []
        for c in combos:
            s = stats_of(tr[mask(tr, c)])
            if s["n"] >= MIN_N[tf] and s["net_clip"] > 0 and s["net_clip"] > base_tr["net_clip"]:
                cands.append((s["net_clip"], c, s))
        cands.sort(key=lambda x: -x[0])
        picked = cands[:top_k]
        res = {"group": grp, "tf": tf, "train_period": (int(tr.born_ts.min()), int(cut)),
               "test_period": (int(cut), int(te.born_ts.max())), "base_train": base_tr, "base_test": base_te,
               "n_candidates_tested": len(combos), "n_passing_train": len(cands), "picked": []}
        for _, c, s in picked:
            sub = te[mask(te, c)]
            st = stats_of(sub)
            lo, hi = boot_ci(sub.net_r.clip(-3, 3).to_numpy())
            per_sym = sub.groupby("symbol").net_r.agg(["size", "mean"]).round(3).to_dict("index")
            res["picked"].append({"filter": " & ".join(c), "train": s, "test": st, "test_ci": (lo, hi),
                                  "test_by_symbol": per_sym})
        out.append(res)
    return out


def fmt(results) -> str:
    import datetime as dt
    day = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
    L = []
    for r in results:
        bt, be = r["base_train"], r["base_test"]
        L.append(f"\n## {r['group']} {r['tf']}  (train {day(r['train_period'][0])}→{day(r['train_period'][1])}, "
                 f"test →{day(r['test_period'][1])})")
        L.append(f"baseline  train n={bt['n']} win={bt['win']:.1f}% netR={bt['net_clip']:+.3f} | "
                 f"test n={be['n']} win={be['win']:.1f}% netR={be['net_clip']:+.3f}")
        L.append(f"{r['n_passing_train']} of {r['n_candidates_tested']} candidate filters beat the baseline on train")
        for p in r["picked"]:
            t, s = p["train"], p["test"]
            lo, hi = p["test_ci"]
            L.append(f"  {p['filter']:<28} train n={t['n']:4} win={t['win']:5.1f}% netR={t['net_clip']:+.3f} | "
                     f"TEST n={s['n']:4} win={s['win']:5.1f}% netR={s['net_clip']:+.3f} CI[{lo:+.2f},{hi:+.2f}]  "
                     + " ".join(f"{k}:{v['mean']:+.2f}(n{v['size']})" for k, v in p["test_by_symbol"].items()))
    return "\n".join(L)


if __name__ == "__main__":
    df = pd.read_pickle(HERE / "setups.pkl")
    res = run(df)
    txt = fmt(res)
    print(txt)
    (HERE / "select_report.txt").write_text(txt)
