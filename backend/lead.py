"""US100 as the lead market: big-move timing, weekend gap, and the SOXL / AMD-NVDA readings that follow it.

Validated out of sample (research/us100_lead.py, US cash session, walk-forward):
  big move ahead   when a large US100 move comes is predictable (5m AUC ≈ 0.73, 1h ≈ 0.63) — not its direction
  weekend gap      the 24/7 perp's move since Friday's close predicts Monday's cash gap (27 of 27 weekends)
Not validated (shown with their test results): US100 direction, SOXL following US100, the AMD / NVDA pair.

The big-move model is fitted at startup on the Yahoo cache (data/bars.sqlite) and applied to the live
Hyperliquid US100 perp bars during the NY cash session (the model only saw cash-session bars).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import data, prob
from .config import LEAD, ROOT, SETTINGS
from .stats import liq_price

ET = ZoneInfo("America/New_York")
CACHE = ROOT / "data" / "bars.sqlite"
REPORT = ROOT / "research" / "us100_lead_report.json"
BIG = {"5m": {"H": 12, "k_atr": 3.0}, "1h": {"H": 3, "k_atr": 1.5}}
FEATS = ["compression", "first_hour", "last_hour", "monday", "rvol", "abs_mom"]
_MODELS: dict = {}


def _atr(b: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = b["high"], b["low"], b["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def mom_z(close: pd.Series, k: int, win: int = 200) -> pd.Series:
    r = np.log(close).diff(k)
    return r / r.rolling(win, min_periods=50).std()


def features(b: pd.DataFrame) -> pd.DataFrame:
    """Big-move features from one market's bars (ts, high, low, close, volume), known at each bar's close."""
    a = _atr(b)
    et = [datetime.fromtimestamp(int(t), ET) for t in b["ts"]]
    mins = np.array([t.hour * 60 + t.minute - 570 for t in et])
    vol = b["volume"].astype(float)
    return pd.DataFrame({
        "compression": (a / a.rolling(100, min_periods=30).mean()).clip(0.3, 3),
        "first_hour": (mins < 60).astype(float), "last_hour": (mins >= 330).astype(float),
        "monday": np.array([t.weekday() == 0 for t in et], float),
        "rvol": np.log(vol / vol.rolling(20, min_periods=5).mean()).replace([np.inf, -np.inf], np.nan).clip(-2, 2),
        "abs_mom": mom_z(b["close"], 6).abs().clip(0, 4),
    }, index=b.index), a, mins


def big_label(b: pd.DataFrame, a: pd.Series, H: int, k_atr: float) -> pd.Series:
    c = b["close"]
    hi = b["high"][::-1].rolling(H, min_periods=H).max()[::-1].shift(-1)
    lo = b["low"][::-1].rolling(H, min_periods=H).min()[::-1].shift(-1)
    return (((hi - c) >= k_atr * a) | ((c - lo) >= k_atr * a)).astype(float).where(hi.notna())


def _cache_bars(sym: str, tf: str) -> pd.DataFrame:
    with sqlite3.connect(CACHE) as con:
        return pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND tf=? ORDER BY ts",
                                 con, params=(sym, tf))


def fit_models() -> None:
    """Fit the big-move model per timeframe on the full cash-session history of the cache."""
    for tf, cfg in BIG.items():
        try:
            b = _cache_bars("US100", tf)
            X, a, _ = features(b)
            y = big_label(b, a, cfg["H"], cfg["k_atr"])
            ok = y.notna().to_numpy() & np.isfinite(X.to_numpy()).all(1)
            _MODELS[tf] = {"model": prob.fit(X.to_numpy(float)[ok], y.to_numpy(float)[ok], l2=10.0),
                           "base": float(y[ok].mean()), "n": int(ok.sum())}
        except Exception:                      # no cache on a fresh install: the meter is simply not shown
            _MODELS.pop(tf, None)


def report() -> dict:
    try:
        return json.loads(REPORT.read_text())
    except (OSError, ValueError):
        return {}


def _big_now(tf: str) -> dict | None:
    m = _MODELS.get(tf)
    if not m:
        return None
    b = data.load_bars("US100", tf, 400)
    b = b[b["closed"]].reset_index(drop=True)
    if len(b) < 120:
        return None
    X, a, mins = features(b)
    x = X.iloc[[-1]].to_numpy(float)
    if not np.isfinite(x).all():
        return None
    cash = bool(data.session_open())
    cfg = BIG[tf]
    return {"tf": tf, "p": float(prob.predict(m["model"], x)[0]), "base": m["base"], "H": cfg["H"], "k_atr": cfg["k_atr"],
            "move": float(cfg["k_atr"] * a.iloc[-1]), "in_session": cash, "ts": int(b["ts"].iloc[-1])}


def _friday_close() -> tuple[float, int] | None:
    """Perp price at the last cash close (16:00 ET) before now, from the 24/7 Hyperliquid 5m bars."""
    now = datetime.now(ET)
    t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    while t > now or t.weekday() >= 5:
        t -= timedelta(days=1)
    ts = int(t.timestamp()) - 300                    # the 15:55 bar closes at 16:00
    b = data.load_bars("US100", "5m", 3000)
    row = b[b["ts"] <= ts].tail(1)
    if row.empty or int(row["ts"].iloc[0]) < ts - 1800:
        return None
    return float(row["close"].iloc[0]), int(t.timestamp())


def weekend_gap(live: dict, soxl_beta: float) -> dict | None:
    """While the cash market is shut: the perp's move since the last cash close ≈ the next open's gap."""
    if data.session_open():
        return None
    fc = _friday_close()
    perp = data.load_bars("US100", "5m", 1)
    if not fc or perp.empty:
        return None
    px = float(perp["close"].iloc[-1])
    gap = px / fc[0] - 1
    q = live.get("US100") or {}
    return {"since": fc[1], "close": fc[0], "perp": px, "gap_pct": gap * 100,
            "omni_mark": q.get("mark"), "soxl_equiv_pct": gap * 100 * soxl_beta,
            "weekend": datetime.now(ET).weekday() >= 5}


def us100_signal(tf: str = "5m") -> dict | None:
    """US100 momentum z over k bars on the live perp: |z| ≥ thr counts as a strong move (the rule tested)."""
    b = data.load_bars("US100", tf, 600)
    b = b[b["closed"]].reset_index(drop=True)
    if len(b) < 120:
        return None
    z = float(mom_z(b["close"], LEAD["k"]).iloc[-1])
    if not np.isfinite(z):
        return None
    d = 1 if z >= LEAD["thr"] else -1 if z <= -LEAD["thr"] else 0
    return {"tf": tf, "z": z, "dir": d, "k": LEAD["k"], "thr": LEAD["thr"]}


def _beta(sym: str, tf: str = "1h", win: int = 300) -> float | None:
    a, b = data.load_bars(sym, tf, win + 5), data.load_bars("US100", tf, win + 5)
    m = a[["ts", "close"]].merge(b[["ts", "close"]], on="ts", suffixes=("", "_u"))
    r = np.log(m[["close", "close_u"]]).diff().dropna()
    return float(r["close"].cov(r["close_u"]) / r["close_u"].var()) if len(r) > 50 and r["close_u"].var() > 0 else None


def pair_view(sig: dict | None, live: dict) -> dict:
    """AMD / NVDA delta pair: equal notional (LEAD lev × margin per leg). Bullish US100 → long the higher beta."""
    ba, bn = _beta("AMD"), _beta("NVDA")
    out = {"beta": {"AMD": ba, "NVDA": bn}, "lev": LEAD["pair_lev"], "margin": LEAD["pair_margin"],
           "notional": LEAD["pair_lev"] * LEAD["pair_margin"]}
    if not sig or not sig["dir"] or ba is None or bn is None:
        out["legs"] = None
        return out
    hi, lo = ("NVDA", "AMD") if bn >= ba else ("AMD", "NVDA")
    lng, sht = (hi, lo) if sig["dir"] == 1 else (lo, hi)
    legs = []
    for sym, d in ((lng, 1), (sht, -1)):
        px = (live.get(sym) or {}).get("mark") or float(data.load_bars(sym, "5m", 1)["close"].iloc[-1])
        legs.append({"symbol": sym, "side": d, "price": px, "liq": liq_price(px, d, LEAD["pair_lev"], SETTINGS)})
    out["legs"] = legs
    out["net_beta"] = (out["beta"][lng] - out["beta"][sht])
    out["edge_per_1pct"] = out["net_beta"] / 100 * out["notional"]     # USDC per 1% US100 move in the call's favour
    return out


def snapshot(live: dict) -> dict:
    rep = report()
    sig = us100_signal("5m")
    sx_beta = _beta("SOXL") or LEAD["soxl_beta"]
    soxl = None
    if sig and sig["dir"]:
        soxl = {"dir": sig["dir"], "beta": sx_beta}
    q100, q30 = live.get("US100") or {}, live.get("US30") or {}
    return {
        "session_open": bool(data.session_open()), "weekday": datetime.now(ET).weekday(),
        "big": {tf: _big_now(tf) for tf in BIG}, "weekend": weekend_gap(live, sx_beta), "signal": sig,
        "us30": {"us100_mark": q100.get("mark"), "us30_mark": q30.get("mark")},
        "soxl": soxl, "soxl_beta": sx_beta, "pair": pair_view(sig, live),
        "validation": {
            "big_move": {tf: (rep.get("tests", {}).get(tf, {}).get("big_move_direction", {}) or {}).get("big_move") for tf in BIG},
            "direction": {tf: (rep.get("tests", {}).get(tf, {}).get("big_move_direction", {}) or {}).get("direction") for tf in BIG},
            "soxl": {tf: (rep.get("tests", {}).get(tf, {}).get("soxl", {}) or {}).get("driver_US100", {}).get("oos") for tf in BIG},
            "pair": {tf: (rep.get("tests", {}).get(tf, {}).get("pair", {}) or {}).get("oos") for tf in BIG},
            "weekend": rep.get("weekend"), "generated": rep.get("generated")},
    }
