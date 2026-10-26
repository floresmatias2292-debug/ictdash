"""Writes research/trend_report.json: out-of-sample validation of the dashboard's TREND mode.
Run:  .venv/bin/python -W ignore research/trend_report.py
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trend_backtest as tb  # noqa: E402
from backend.config import SETTINGS  # noqa: E402


def split(df):
    if not len(df):
        return {"all": {"n": 0}, "train": {"n": 0}, "test": {"n": 0}}
    cut = df.entry_ts.quantile(0.6)
    return {"all": tb.summary(df), "train": tb.summary(df[df.entry_ts <= cut]), "test": tb.summary(df[df.entry_ts > cut])}


def main():
    out = {"generated": int(time.time()), "rule": "thr 0.35, live weight 0.3, stop 1.5×ATR1h, target 2R, 8 h time stop",
           "groups": {}}
    for g in ("semis", "macro"):
        tb.HOLD_HOURS = 8
        r = {"trend": split(tb.simulate(g, "proxy1h")),
             "flipped": split(tb.simulate(g, "proxy1h", flip=True)),
             "structure_only": split(tb.simulate(g, "proxy1h", w_live=0.0)),
             "exact5m": tb.summary(tb.simulate(g, "exact5m"))}
        grid = []
        for hold, thr, rr in itertools.product((8, 24, 72, 120), (0.25, 0.35, 0.45), (1.0, 2.0)):
            tb.HOLD_HOURS = hold
            s = split(tb.simulate(g, "proxy1h", thr=thr, S={**SETTINGS, "rr": rr}))
            grid.append({"hold": hold, "thr": thr, "rr": rr, "train": s["train"], "test": s["test"]})
        r["grid"] = {"combos": len(grid),
                     "train_positive": sum(1 for x in grid if x["train"].get("net", -1) > 0),
                     "test_ci_above_zero": sum(1 for x in grid if x["test"].get("ci", (-1, -1))[0] > 0)}
        out["groups"][g] = r
    (HERE / "trend_report.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({g: {"trend_all": v["trend"]["all"], "trend_test": v["trend"]["test"], "grid": v["grid"]}
                      for g, v in out["groups"].items()}, indent=1))


if __name__ == "__main__":
    main()
