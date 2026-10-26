"""Out-of-sample validation of the WTI / Brent delta-neutral pair (backend/pair.py).

Walk-forward: the first 40% of history is the first training set; the rest is split into 6 blocks.
Before each block the parameters (timeframe, window, entry z, exit z, hedge) are chosen on everything
before it, then traded unchanged on the block. Only block trades count, so every number is out of sample.
Costs: Omni swap fee + slippage + spread on both legs, both sides. Funding: today's Omni rates held
constant (a scenario — past rates are not published), reported separately.
Writes research/pair_report_<mode>.json.  Run: .venv/bin/python -W ignore research/pair_validate.py [revert|trend]
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import asyncio
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from backend import data, pair, stats  # noqa: E402
from backend.config import PAIRS, SETTINGS  # noqa: E402

GRID = {"tf": ["15m", "1h"], "window": [96, 192, 384], "z_in": [1.5, 2.0, 2.5], "z_out": [0.0, 0.5], "hedge": ["beta", "dollar"]}
MODE = sys.argv[1] if len(sys.argv) > 1 else "revert"      # "revert" (classic pair) or "trend"
BLOCKS, START = 6, 0.4
MIN_TRADES = 15


def params(tf, window, z_in, z_out, hedge, mode=None):
    return {"tf": tf, "window": window, "z_in": z_in, "z_out": z_out, "z_stop": z_in + 1.5,
            "max_hold": window // 2, "hedge": hedge, "mode": mode or MODE}


def score(trades):
    """t-statistic of the mean net return: rewards an edge that is both large and consistent."""
    if len(trades) < MIN_TRADES:
        return -np.inf
    r = np.array([t["net"] for t in trades])
    return r.mean() / (r.std(ddof=1) / np.sqrt(len(r)) + 1e-12)


def with_funding(trades, live, cfg):
    out = []
    a, b = cfg["a"], cfg["b"]
    ma = {"kind": "swap", "f_long": live[a]["funding_annual_long"], "f_short": live[a]["funding_annual_short"]}
    mb = {"kind": "swap", "f_long": live[b]["funding_annual_long"], "f_short": live[b]["funding_annual_short"]}
    for t in trades:
        fa = stats.funding_frac(ma, t["dir"], t["entry_ts"], t["exit_ts"], SETTINGS)
        fb = stats.funding_frac(mb, -t["dir"], t["entry_ts"], t["exit_ts"], SETTINGS) * t["beta"]
        out.append({**t, "net": t["net"] - fa - fb})
    return out


def main():
    cfg = PAIRS["oil"]
    pa, pb = cfg["proxy"][cfg["a"]], cfg["proxy"][cfg["b"]]
    cost2 = 2 * pair.leg_costs(SETTINGS, cfg["spread_bps"], SETTINGS[cfg["fee_key"]])   # both legs, β = 1
    print(f"round-trip cost, both legs: {cost2 * 100:.3f}% of one leg's notional")

    # all trades per parameter set (spread statistics are causal, so one pass over the full history)
    sims, span = {}, {}
    for tf in GRID["tf"]:
        df = pair.aligned(pa, pb, tf, limit=10 ** 6)
        span[tf] = (int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]))
        for w, h in itertools.product(GRID["window"], GRID["hedge"]):
            st = pair.spread_stats(df, w, h)
            for zi, zo in itertools.product(GRID["z_in"], GRID["z_out"]):
                p = params(tf, w, zi, zo, h)
                sims[tuple(p.values())] = (p, pair.simulate(st, p, cost2))
    t0, t1 = max(s[0] for s in span.values()), min(s[1] for s in span.values())
    edges = np.linspace(t0 + START * (t1 - t0), t1, BLOCKS + 1)

    oos, picks = [], []
    for k in range(BLOCKS):
        lo, hi = edges[k], edges[k + 1]
        best, best_s = None, -np.inf
        for key, (p, tr) in sims.items():
            train = [t for t in tr if t["exit_ts"] < lo]          # fully closed before the block starts
            s = score(train)
            if s > best_s:
                best, best_s = key, s
        if best is None:
            continue
        p, tr = sims[best]
        blk = [t for t in tr if lo <= t["entry_ts"] < hi]
        oos += blk
        picks.append({"block": k, "from": int(lo), "to": int(hi), "params": p, "train_t": float(best_s),
                      "oos": pair.summary(blk)})
        print(f"block {k} {datetime.utcfromtimestamp(lo):%m-%d}→{datetime.utcfromtimestamp(hi):%m-%d} "
              f"pick {p['tf']} w{p['window']} z{p['z_in']}/{p['z_out']} {p['hedge']:6} train t={best_s:.2f} | "
              f"oos {pair.summary(blk)}")

    async def live():
        async with httpx.AsyncClient() as c:
            return await data.fetch_omni(c)
    lv = asyncio.run(live())
    res = {"oos": pair.summary(oos), "oos_funding_today": pair.summary(with_funding(oos, lv, cfg))}
    fixed = sims[tuple(params(**{k: v for k, v in pair.DEFAULT.items() if k in GRID}).values())]
    fixed_oos = [t for t in fixed[1] if t["entry_ts"] >= edges[0]]
    res["default_oos"] = {"params": fixed[0], **pair.summary(fixed_oos)}
    # the in-sample best, to show how much a grid search flatters itself
    ins = max(sims.values(), key=lambda v: score(v[1]))
    res["in_sample_best"] = {"params": ins[0], **pair.summary(ins[1])}
    print("OOS walk-forward      :", res["oos"])
    print("OOS + today's funding :", res["oos_funding_today"])
    print("default params, OOS   :", res["default_oos"])
    print("in-sample best (biased):", res["in_sample_best"])
    by_dir = {d: pair.summary([t for t in oos if t["dir"] == d]) for d in (1, -1)}
    print("OOS by direction (+1 long WTI/short Brent):", by_dir)
    out = {"generated": int(time.time()), "mode": MODE, "cost_round_trip_pct": cost2 * 100, "period": [int(t0), int(t1)],
           "oos_from": int(edges[0]), "blocks": picks, "by_dir": {str(k): v for k, v in by_dir.items()},
           "funding_today": {leg: {"long": lv[leg]["funding_annual_long"], "short": lv[leg]["funding_annual_short"]}
                             for leg in (cfg["a"], cfg["b"])}, **res}
    (HERE / f"pair_report_{MODE}.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
