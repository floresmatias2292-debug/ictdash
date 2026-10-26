"""Setup dataset with filter features, for out-of-sample filter research.

One row per FVG setup that actually traded (win / loss / timeout), with:
  align_own  setup direction == market structure trend on its own timeframe at the setup bar
  align_1h   same, using the 1h trend known at that moment (closed 1h bars only)
  align_4h   same, using the 4h trend known at that moment (closed 4h bars only)
  session    open (bar closes ≤ 10:30 ET) · mid (10:30–14:00) · late (≥ 14:00)
  size_atr   FVG height in ATRs
  comps      the five Pine confluences (strong, pd, sweep, mss, ob)
  win, r     outcome at the configured target (gross R) and net_r = r − round-trip costs in R
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import data, ict, stats  # noqa: E402
from backend.config import GROUPS, SETTINGS, SYMBOLS, TIMEFRAMES  # noqa: E402

ET = ZoneInfo("America/New_York")


def _trend_asof(bars_hi: pd.DataFrame, trend_hi: np.ndarray, tf_hi: str, when: np.ndarray) -> np.ndarray:
    """Trend of the higher timeframe from the last bar that had closed by `when`."""
    ends = np.array([data.bar_end(int(t), tf_hi) for t in bars_hi["ts"]])
    idx = np.searchsorted(ends, when, side="right") - 1
    out = np.zeros(len(when), dtype=int)
    ok = idx >= 0
    out[ok] = trend_hi[idx[ok]]
    return out


def build(S: dict | None = None) -> pd.DataFrame:
    S = S or SETTINGS
    rows = []
    for grp, cfg in GROUPS.items():
        fee = S[cfg["fee_key"]]
        for sym in cfg["symbols"]:
            loaded = {}
            for tf in TIMEFRAMES:
                b = data.load_bars(sym, tf)
                b = b[b["closed"]].reset_index(drop=True)
                loaded[tf] = (b, ict.run(b, tf))
            for tf in TIMEFRAMES:
                b, res = loaded[tf]
                setups = stats.simulate_setups(b, res, tf, sym, S)
                done = [s for s in setups if s["status"] in ("win", "loss", "timeout")]
                if not done:
                    continue
                close_at = np.array([data.bar_end(s["born_ts"], tf) for s in done])
                t1h = _trend_asof(loaded["1h"][0], loaded["1h"][1]["trend"], "1h", close_at)
                t4h = _trend_asof(loaded["4h"][0], loaded["4h"][1]["trend"], "4h", close_at)
                fvg = {z["id"]: z for z in res["fvgs"]}
                cost = stats.costs_pct(S, SYMBOLS[sym]["spread_bps"], fee)
                for k, s in enumerate(done):
                    z = fvg[s["fvg"]]
                    t = datetime.fromtimestamp(int(close_at[k]), ET)
                    m = t.hour * 60 + t.minute
                    stop_pct = abs(s["fill_px"] - s["stop"]) / s["fill_px"]
                    rows.append({
                        "group": grp, "symbol": sym, "tf": tf, "born_ts": s["born_ts"], "dir": s["dir"],
                        "score": s["score"], **{f"c_{c}": v for c, v in s["comps"].items()},
                        "align_own": int(res["trend"][s["born"]] == s["dir"]),
                        "align_1h": int(t1h[k] == s["dir"]), "align_4h": int(t4h[k] == s["dir"]),
                        "counter_4h": int(t4h[k] == -s["dir"]),
                        "session": "open" if m <= 630 else "mid" if m < 840 else "late",
                        "size_atr": (z["top"] - z["bot"]) / z["atr"] if z["atr"] else np.nan,
                        "stop_pct": stop_pct, "win": int(s["status"] == "win"), "r": s["r"],
                        "cost_r": cost / stop_pct if stop_pct > 0 else np.nan,
                    })
    df = pd.DataFrame(rows)
    df["net_r"] = df["r"] - df["cost_r"]
    return df


if __name__ == "__main__":
    df = build()
    out = Path(__file__).resolve().parent / "setups.parquet"
    df.to_pickle(out.with_suffix(".pkl"))
    print(df.groupby(["group", "tf"]).agg(n=("win", "size"), win=("win", "mean"), net_r=("net_r", "mean"),
                                          align_4h=("align_4h", "mean")).round(3))
