"""Extra feature families for the long/short probability, all computed from information known at the bar's close.

Families
  flow     signed-volume flow (OBV slope), volume-weighted close location (A/D), distance to session VWAP
  time     minutes since the open, first 30 min, last hour, Monday, Friday
  daily    overnight gap, position vs prior-day high/low/close, share of the average daily range used,
           close vs 20-day average (daily trend), opening-drive return
  cross    the other group members' recent returns (lead/lag) and the group's mean move
  meanrev  RSI(14), z-score vs 20-bar mean, Bollinger %B
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")

FAMILIES = {
    "flow": ["obv_slope", "ad_flow", "vwap_dist", "flow_x_rvol"],
    "time": ["mins_open", "first30", "last60", "monday", "friday"],
    "daily": ["gap", "vs_pdh", "vs_pdl", "vs_pdc", "adr_used", "d_trend", "open_drive"],
    "cross": ["peer_r1", "peer_r3", "peer_r12", "group_r3", "rel_r3"],
    "meanrev": ["rsi", "z20", "bb_pct"],
}


def per_symbol(b: pd.DataFrame, atr: np.ndarray) -> pd.DataFrame:
    o, h, l, c, v = (b[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    n = len(c)
    et = [datetime.fromtimestamp(int(t), ET) for t in b["ts"]]
    day = np.array([t.date().toordinal() for t in et])
    mins = np.array([t.hour * 60 + t.minute for t in et]) - 570
    wd = np.array([t.weekday() for t in et])
    s = lambda x: pd.Series(x)
    with np.errstate(divide="ignore", invalid="ignore"):
        rng = np.where(h - l > 0, h - l, np.nan)
        clv = ((c - l) - (h - c)) / rng
        sv = np.sign(c - o) * v
        vma = s(v).rolling(20, min_periods=5).mean().to_numpy()
        obv_slope = s(sv).rolling(12, min_periods=4).sum().to_numpy() / (vma * 12)
        ad_flow = s(clv * v).rolling(12, min_periods=4).sum().to_numpy() / (vma * 12)
        tp = (h + l + c) / 3
        g = pd.DataFrame({"d": day, "pv": tp * v, "v": v})
        vwap = (g.groupby("d")["pv"].cumsum() / g.groupby("d")["v"].cumsum()).to_numpy()
        vwap_dist = np.clip((c - vwap) / atr, -5, 5)
        rvol = np.log(np.where(vma > 0, v / vma, np.nan))
        # daily context from the bars themselves (regular session)
        D = pd.DataFrame({"d": day, "o": o, "h": h, "l": l, "c": c}).groupby("d").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
        D["pdh"], D["pdl"], D["pdc"] = D["h"].shift(1), D["l"].shift(1), D["c"].shift(1)
        D["adr"] = (D["h"] - D["l"]).shift(1).rolling(14, min_periods=5).mean()
        D["ma20"] = D["c"].shift(1).rolling(20, min_periods=10).mean()
        dd = D.reindex(day)
        run_hi = g.assign(h=h).groupby("d")["h"].cummax().to_numpy()
        run_lo = g.assign(l=l).groupby("d")["l"].cummin().to_numpy()
        adr = dd["adr"].to_numpy()
        gap = np.clip((dd["o"].to_numpy() - dd["pdc"].to_numpy()) / adr, -3, 3)
        vs = lambda ref: np.clip((c - ref) / adr, -3, 3)
        # Opening drive: close of the last bar that ENDED by 10:00 ET, used only once that bar has closed.
        # (1h bars end at 10:30 at the earliest, so on 1h the feature is always 0 — no lookahead.)
        bar_min = int(np.median(np.diff(b["ts"].to_numpy())) // 60) if n > 1 else 5
        ends = mins + bar_min
        drive_px = pd.Series(np.where(ends <= 30, c, np.nan)).groupby(day).ffill().to_numpy()
        open_drive = np.where(ends > 30, np.clip(np.nan_to_num((drive_px - dd["o"].to_numpy()) / adr), -3, 3), 0.0)
        # mean reversion
        d1 = np.diff(c, prepend=c[0])
        up = s(np.maximum(d1, 0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        dn = s(np.maximum(-d1, 0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        rsi = np.where(up + dn > 0, up / (up + dn), 0.5) - 0.5
        m20, sd20 = s(c).rolling(20).mean().to_numpy(), s(c).rolling(20).std().to_numpy()
        z20 = np.clip((c - m20) / sd20, -4, 4)
        bb = np.clip((c - (m20 - 2 * sd20)) / (4 * sd20) - 0.5, -1.5, 1.5)
        r1 = np.concatenate([[np.nan], np.diff(c)]) / atr
        r3 = (c - np.concatenate([np.full(3, np.nan), c[:-3]])) / atr
        r12 = (c - np.concatenate([np.full(12, np.nan), c[:-12]])) / atr
    return pd.DataFrame({
        "ts": b["ts"].to_numpy(),
        "obv_slope": np.clip(obv_slope, -3, 3), "ad_flow": np.clip(ad_flow, -3, 3), "vwap_dist": vwap_dist,
        "flow_x_rvol": np.clip(obv_slope, -3, 3) * np.clip(np.nan_to_num(rvol), -2, 2),
        "mins_open": mins / 390, "first30": (mins < 30).astype(float), "last60": (mins >= 330).astype(float),
        "monday": (wd == 0).astype(float), "friday": (wd == 4).astype(float),
        "gap": gap, "vs_pdh": vs(dd["pdh"].to_numpy()), "vs_pdl": vs(dd["pdl"].to_numpy()), "vs_pdc": vs(dd["pdc"].to_numpy()),
        "adr_used": np.clip((run_hi - run_lo) / adr, 0, 3), "d_trend": np.clip((c - dd["ma20"].to_numpy()) / adr, -5, 5),
        "open_drive": open_drive, "rsi": rsi, "z20": z20, "bb_pct": bb,
        "_r1": np.clip(r1, -5, 5), "_r3": np.clip(r3, -5, 5), "_r12": np.clip(r12, -8, 8),
    })


def add_cross(df: pd.DataFrame) -> pd.DataFrame:
    """Peers' returns at the same timestamp (bars that close together)."""
    out = []
    for sym, g in df.groupby("symbol"):
        peers = df[df["symbol"] != sym].groupby("ts")[["_r1", "_r3", "_r12"]].mean()
        allm = df.groupby("ts")["_r3"].mean()
        j = g.join(peers, on="ts", rsuffix="_p")
        j["peer_r1"], j["peer_r3"], j["peer_r12"] = j["_r1_p"], j["_r3_p"], j["_r12_p"]
        j["group_r3"] = j["ts"].map(allm)
        j["rel_r3"] = j["_r3"] - j["peer_r3"]
        out.append(j.drop(columns=["_r1_p", "_r3_p", "_r12_p"]))
    return pd.concat(out).sort_index()
