"""Port of the Pine indicator "FVG ICT — Confluencias (Sweep · MSS · OB · P/D · OR)".

Runs bar by bar in the same order as the Pine script, so each bar only sees the past:
  1. pivots → 2. sweeps / BOS / CHoCH → 3. FVG mitigation + OB break → 4. new FVG + OB + score
  → 5. Opening Range. Premium/Discount and the info table are computed on the last bar.
Only closed bars should be passed in.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .config import ICT_DEFAULTS
from .data import bar_end

ET = ZoneInfo("America/New_York")


def _rma_atr(h, l, c, n):
    tr = np.empty(len(h))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = np.full(len(h), np.nan)
    if len(h) >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, len(h)):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def _rolling(a, n, fn):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n - 1:] = fn(w, axis=1)
    return out


def _pivots(a, L, high=True):
    """ta.pivothigh/pivotlow(a, L, L): value at i-L, confirmed at bar i.
    Center must beat the left side strictly and be >= the right side (ties resolve to the first bar)."""
    out = np.full(len(a), np.nan)
    for i in range(2 * L, len(a)):
        c = a[i - L]
        left, right = a[i - 2 * L:i - L], a[i - L + 1:i + 1]
        if high and c > left.max() and c >= right.max():
            out[i] = c
        elif not high and c < left.min() and c <= right.min():
            out[i] = c
    return out


def run(bars, tf: str, params: dict | None = None) -> dict:
    p = {**ICT_DEFAULTS, **(params or {})}
    ts = bars["ts"].to_numpy(np.int64)
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    l = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    n = len(c)

    atr = _rma_atr(h, l, c, p["atr_len"])
    if p["size_mode"] == "ATR":
        min_size, strong_size = atr * p["min_atr"], atr * p["strong_atr"]
    else:
        min_size, strong_size = c * p["min_pct"] / 100, c * p["strong_pct"] / 100
    pd_hi = _rolling(h, p["pd_len"], np.max)
    pd_lo = _rolling(l, p["pd_len"], np.min)
    eq = (pd_hi + pd_lo) / 2
    L = p["swing_len"]
    ph, pl = _pivots(h, L, True), _pivots(l, L, False)

    # State (Pine `var`s)
    swH = swL = math.nan
    swHi = swLi = -1
    swHSwept = swLSwept = False
    swHBroken = swLBroken = True
    trend = 0
    lastBSL = lastSSL = lastBullShift = lastBearShift = None

    fvg_all, ob_all = [], []
    active_f, active_o = [], []       # indexes into fvg_all / ob_all
    sweeps, structure, ors = [], [], []
    trend_arr = np.zeros(n, dtype=int)
    events = []                       # per-bar alert events
    # Per-bar snapshot of the indicator state (what the Pine script knows at that bar's close).
    BIG = 999
    st_since = {k: np.full(n, BIG, dtype=float) for k in ("ssl", "bsl", "bull_choch", "bear_choch", "bos_up", "bos_dn")}
    st_dist = {k: np.full(n, np.nan) for k in ("bull_fvg", "bear_fvg", "bull_ob", "bear_ob")}
    st_cnt = {k: np.zeros(n) for k in ("bull_fvg", "bear_fvg", "bull_ob", "bear_ob")}
    st_or = np.zeros(n)               # +1 above today's OR high, −1 below its low, 0 inside / none
    st_new = {k: np.zeros(n) for k in ("bull_fvg_score", "bear_fvg_score")}
    lastBosUp = lastBosDn = None

    # Opening range state
    or_cur = None
    prev_in_or = False
    mm = p["mit_mode"]

    for i in range(n):
        # 1. pivots
        if not math.isnan(ph[i]):
            swH, swHi, swHSwept, swHBroken = ph[i], i - L, False, False
        if not math.isnan(pl[i]):
            swL, swLi, swLSwept, swLBroken = pl[i], i - L, False, False

        # 2. sweeps & structure
        bsl = not math.isnan(swH) and not swHSwept and not swHBroken and h[i] > swH and c[i] <= swH
        ssl = not math.isnan(swL) and not swLSwept and not swLBroken and l[i] < swL and c[i] >= swL
        bull_break = not math.isnan(swH) and not swHBroken and c[i] > swH
        bear_break = not math.isnan(swL) and not swLBroken and c[i] < swL

        if bsl:
            swHSwept, lastBSL = True, i
            sweeps.append({"kind": "BSL", "i": i, "from": swHi, "price": swH})
            events.append({"i": i, "type": "sweep", "dir": -1, "text": "BSL sweep"})
        if ssl:
            swLSwept, lastSSL = True, i
            sweeps.append({"kind": "SSL", "i": i, "from": swLi, "price": swL})
            events.append({"i": i, "type": "sweep", "dir": 1, "text": "SSL sweep"})
        if bull_break:
            shift = trend == -1
            swHBroken = True
            if shift:
                lastBullShift = i
                events.append({"i": i, "type": "choch", "dir": 1, "text": "CHoCH ▲"})
            trend = 1
            if not shift:
                lastBosUp = i
            structure.append({"kind": "CHoCH" if shift else "BOS", "dir": 1, "i": i, "from": swHi, "price": swH})
        if bear_break:
            shift = trend == 1
            swLBroken = True
            if shift:
                lastBearShift = i
                events.append({"i": i, "type": "choch", "dir": -1, "text": "CHoCH ▼"})
            trend = -1
            if not shift:
                lastBosDn = i
            structure.append({"kind": "CHoCH" if shift else "BOS", "dir": -1, "i": i, "from": swLi, "price": swL})
        trend_arr[i] = trend

        # 3. mitigation of FVGs, break of OBs (before creating new ones)
        for k in range(len(active_f) - 1, -1, -1):
            z = fvg_all[active_f[k]]
            mid = (z["top"] + z["bot"]) / 2
            if z["bull"]:
                hit = (l[i] <= z["top"] if mm == "Toque" else l[i] <= mid if mm == "50% (CE)"
                       else l[i] <= z["bot"] if mm == "Completo (mecha)" else c[i] < z["bot"])
            else:
                hit = (h[i] >= z["bot"] if mm == "Toque" else h[i] >= mid if mm == "50% (CE)"
                       else h[i] >= z["top"] if mm == "Completo (mecha)" else c[i] > z["top"])
            if hit:
                z["end"], z["state"] = i, "mitigated"
                active_f.pop(k)
        for k in range(len(active_o) - 1, -1, -1):
            ob = ob_all[active_o[k]]
            if (c[i] < ob["bot"]) if ob["bull"] else (c[i] > ob["top"]):
                ob["end"], ob["state"] = i, "broken"
                active_o.pop(k)

        # 4. new FVG + OB + confluence score
        if i >= 2 and not math.isnan(min_size[i]):
            for bull in (True, False):
                if bull:
                    gap, sz = l[i] > h[i - 2], l[i] - h[i - 2]
                    f_top, f_bot = l[i], h[i - 2]
                else:
                    gap, sz = h[i] < l[i - 2], l[i - 2] - h[i]
                    f_top, f_bot = l[i - 2], h[i]
                if not gap or sz < min_size[i]:
                    continue
                ob_k = None
                for k in range(2, 2 + p["ob_lookback"] + 1):
                    j = i - k
                    if j < 0:
                        break
                    if (c[j] < o[j]) if bull else (c[j] > o[j]):
                        ob_k = k
                        if bull:
                            ob_top = o[j] if p["ob_body"] else h[j]
                            ob_bot = c[j] if p["ob_body"] else l[j]
                        else:
                            ob_top = c[j] if p["ob_body"] else h[j]
                            ob_bot = o[j] if p["ob_body"] else l[j]
                        break
                mid = (f_top + f_bot) / 2
                last_sweep = lastSSL if bull else lastBSL
                last_shift = lastBullShift if bull else lastBearShift
                comps = {
                    "strong": bool(sz >= strong_size[i]),
                    "pd": bool(not math.isnan(eq[i]) and (mid < eq[i] if bull else mid > eq[i])),
                    "sweep": bool(last_sweep is not None and i - last_sweep <= p["conf_look"]),
                    "mss": bool(last_shift is not None and i - last_shift <= p["conf_look"]),
                    "ob": ob_k is not None,
                }
                score = sum(comps.values())
                z = {"id": len(fvg_all), "bull": bull, "top": float(f_top), "bot": float(f_bot),
                     "born": i, "left": i - 2, "end": None, "state": "active", "score": score,
                     "comps": comps, "size_pct": float(sz / c[i] * 100), "atr": float(atr[i])}
                fvg_all.append(z)
                active_f.append(z["id"])
                if len(active_f) > p["max_active"]:
                    old = fvg_all[active_f.pop(0)]
                    old["end"], old["state"] = i, "expired"
                if ob_k is not None:
                    ob = {"id": len(ob_all), "bull": bull, "top": float(ob_top), "bot": float(ob_bot),
                          "born": i, "left": i - ob_k, "end": None, "state": "active", "fvg": z["id"]}
                    ob_all.append(ob)
                    active_o.append(ob["id"])
                    if len(active_o) > p["max_active"]:
                        old = ob_all[active_o.pop(0)]
                        old["end"], old["state"] = i, "expired"
                st_new["bull_fvg_score" if bull else "bear_fvg_score"][i] = score + 1   # 1..6 (0 = none)
                events.append({"i": i, "type": "fvg", "dir": 1 if bull else -1, "fvg": z["id"], "score": score,
                               "text": f"{'▲' if bull else '▼'} FVG {score}/5"})

        # 5. Opening Range 09:30 NY
        d = datetime.fromtimestamp(int(ts[i]), ET)
        or_start = int(d.replace(hour=9, minute=30, second=0, microsecond=0).timestamp())
        or_end = or_start + p["or_minutes"] * 60
        in_or = bar_end(int(ts[i]), tf) > or_start and ts[i] < or_end
        if in_or and not prev_in_or:
            or_cur = {"start": i, "end": i, "high": float(h[i]), "low": float(l[i]), "done": False}
            ors.append(or_cur)
        elif in_or and or_cur:
            or_cur["high"] = max(or_cur["high"], float(h[i]))
            or_cur["low"] = min(or_cur["low"], float(l[i]))
            or_cur["end"] = i
        if not in_or and prev_in_or and or_cur:
            or_cur["end"] = or_cur["start"] + p["or_extend"]
            or_cur["done"] = True
        prev_in_or = in_or

        # 6. state snapshot for the probability model
        for key, last in (("ssl", lastSSL), ("bsl", lastBSL), ("bull_choch", lastBullShift),
                          ("bear_choch", lastBearShift), ("bos_up", lastBosUp), ("bos_dn", lastBosDn)):
            if last is not None:
                st_since[key][i] = i - last
        a = atr[i]
        if a and not math.isnan(a):
            for kind, pool, lst in (("bull_fvg", fvg_all, active_f), ("bear_fvg", fvg_all, active_f),
                                    ("bull_ob", ob_all, active_o), ("bear_ob", ob_all, active_o)):
                want_bull = kind.startswith("bull")
                best = math.inf
                cnt = 0
                for k in lst:
                    z = pool[k]
                    if z["bull"] != want_bull:
                        continue
                    cnt += 1
                    dd = 0.0 if z["bot"] <= c[i] <= z["top"] else min(abs(c[i] - z["top"]), abs(c[i] - z["bot"]))
                    best = min(best, dd)
                st_cnt[kind][i] = cnt
                if cnt:
                    st_dist[kind][i] = best / a
        if ors and ors[-1]["start"] <= i and datetime.fromtimestamp(int(ts[ors[-1]["start"]]), ET).date() == d.date():
            o_ = ors[-1]
            st_or[i] = 1 if c[i] > o_["high"] else -1 if c[i] < o_["low"] else 0

    # Premium / Discount + table (last bar)
    last = n - 1
    pd_info = None
    table = {}
    if n and not math.isnan(pd_hi[last]):
        rng = pd_hi[last] - pd_lo[last]
        pos = (c[last] - pd_lo[last]) / rng * 100 if rng > 0 else 50.0
        pd_info = {"hi": float(pd_hi[last]), "lo": float(pd_lo[last]), "eq": float(eq[last]),
                   "x0": max(0, last - p["pd_len"]), "pos": float(pos),
                   "zone": "Premium" if pos > 55 else "Discount" if pos < 45 else "Equilibrium"}
    if n:
        def ago(x):
            return None if x is None else last - x
        sweep_kind = None
        if lastBSL is not None or lastSSL is not None:
            sweep_kind = "BSL" if (lastBSL if lastBSL is not None else -1) >= (lastSSL if lastSSL is not None else -1) else "SSL"
        shift_dir = None
        if lastBullShift is not None or lastBearShift is not None:
            shift_dir = 1 if (lastBullShift if lastBullShift is not None else -1) >= (lastBearShift if lastBearShift is not None else -1) else -1
        act_f = [fvg_all[k] for k in active_f]
        act_o = [ob_all[k] for k in active_o]
        table = {
            "atr": float(atr[last]) if not math.isnan(atr[last]) else None,
            "min_fvg_pct": float(min_size[last] / c[last] * 100) if not math.isnan(min_size[last]) else None,
            "trend": int(trend),
            "pd": pd_info,
            "fvg_bull": sum(z["bull"] for z in act_f), "fvg_bear": sum(not z["bull"] for z in act_f),
            "ob_bull": sum(z["bull"] for z in act_o), "ob_bear": sum(not z["bull"] for z in act_o),
            "last_sweep": sweep_kind, "last_sweep_ago": ago(max(x for x in (lastBSL, lastSSL, -1) if x is not None)) if sweep_kind else None,
            "last_shift": shift_dir, "last_shift_ago": ago(max(x for x in (lastBullShift, lastBearShift, -1) if x is not None)) if shift_dir else None,
        }

    return {"fvgs": fvg_all, "obs": ob_all, "sweeps": sweeps, "structure": structure, "ors": ors,
            "pd": pd_info, "table": table, "events": events, "trend": trend_arr, "atr": atr,
            "state": {"since": st_since, "dist": st_dist, "count": st_cnt, "or_pos": st_or, "new": st_new,
                      "pd_pos": np.where(pd_hi - pd_lo > 0, (c - pd_lo) / (pd_hi - pd_lo), np.nan)}}
