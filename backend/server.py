"""FastAPI app: FVG-ICT charts (5m / 15m / 1h) with live long/short probabilities.

Every closed bar: run the Pine port, build the probability features, refit the model per group and
timeframe on all labeled history, and predict the latest bar. Omni marks stream every 5 s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import data, ict, loris, prob, social, stats
from .config import (FRONTEND_DIR, GROUPS, ICT_DEFAULTS, ORACLE_EVERY_S, SETTINGS, SOURCE_LABEL, SYMBOLS,
                     TRADINGVIEW_SYMBOL)
from .portfolio import fee_pct, max_lev

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("server")
ET = ZoneInfo("America/New_York")

VIEW_TFS = ["5m", "15m", "1h"]
HTF = {"5m": "1h", "15m": "1h", "1h": "4h"}
CALC_TFS = ["5m", "15m", "1h", "4h"]
REPORT = FRONTEND_DIR.parent / "research" / "prob_report.json"

STATE: dict = {}          # (symbol, tf) -> {bars, forming, res, prob}
LIVE: dict = {}           # symbol -> Omni quote
LORIS: dict = {}          # symbol -> funding by venue from Loris Tools
ORACLE: dict = {}         # symbol -> {consensus (CoinGecko), venue (Binance/Hyperliquid index), last}
ALERTS: list = []
META = {"loading": False, "history_error": {}, "version": 0, "last_calc": None, "last_candles": None, "candle_error": None, "last_omni": None, "omni_error": None}
_recalc_lock = asyncio.Lock()
CLIENTS: set[WebSocket] = set()
_last_closed: dict = {}


def _report() -> dict:
    try:
        return json.loads(REPORT.read_text())
    except (OSError, ValueError):
        return {}


# ── recompute ──────────────────────────────────────────────────────────────
def recompute() -> None:
    t0 = time.time()
    new = {}
    for sym in SYMBOLS:
        for tf in CALC_TFS:
            allb = data.load_bars(sym, tf)
            bars = allb[allb["closed"]].reset_index(drop=True)
            if len(bars) < 60:
                continue
            new[(sym, tf)] = {"bars": bars, "forming": allb[~allb["closed"]], "res": ict.run(bars, tf)}
    rep = _report().get("groups", {})
    for g, cfg in GROUPS.items():
        for tf in VIEW_TFS:
            frames, last_rows = [], {}
            for sym in cfg["symbols"]:
                st, hst = new.get((sym, tf)), new.get((sym, HTF[tf]))
                if not st or not hst:
                    continue
                b, res = st["bars"], st["res"]
                ends = np.array([data.bar_end(int(t), HTF[tf]) for t in hst["bars"]["ts"]])
                when = np.array([data.bar_end(int(t), tf) for t in b["ts"]])
                idx = np.searchsorted(ends, when, side="right") - 1
                htf = np.where(idx >= 0, hst["res"]["trend"][np.clip(idx, 0, None)], 0).astype(float)
                f = pd.concat([prob.features(b, res, htf, tf), prob.labels(b, res["atr"], tf)], axis=1).iloc[50:]
                frames.append(f)
                last_rows[sym] = f.iloc[[-1]]
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)
            lab = df["y"].notna()
            model = prob.fit(df.loc[lab, prob.FEATURES].to_numpy(float), df.loc[lab, "y"].to_numpy(float))
            skill = (rep.get(g) or {}).get(tf)
            contrib_names = prob.FEATURES
            for sym, row in last_rows.items():
                x = row[prob.FEATURES].to_numpy(float)
                p_up = float(prob.predict(model, x)[0])
                z = np.nan_to_num((x[0] - model["mu"]) / model["sd"])
                contrib = sorted(zip(contrib_names, (model["w"][1:] * z).tolist()), key=lambda t: -abs(t[1]))[:5]
                new[(sym, tf)]["prob"] = {"p_up": p_up, "base_up": float(df.loc[lab, "y"].mean()),
                                          "n_train": int(lab.sum()), "skill": skill,
                                          "drivers": [{"feature": k, "logit": v} for k, v in contrib],
                                          "atr": float(row["atr"].iloc[0]), "close": float(row["close"].iloc[0])}
    STATE.clear()
    STATE.update(new)
    _collect_alerts()
    META["version"] += 1
    META["last_calc"] = time.time()
    log.info("recompute v%s in %.2fs", META["version"], time.time() - t0)


def _signal(sym: str, tf: str) -> dict:
    """Long/short call from the probability, sized at risk_pct with 1×ATR stop and target (the validated barrier)."""
    p = (STATE.get((sym, tf)) or {}).get("prob")
    if not p:
        return {"side": None}
    thr = SETTINGS["signal_prob"] / 100
    side = 1 if p["p_up"] >= thr else -1 if p["p_up"] <= 1 - thr else 0
    out = {"side": side, "p_long": p["p_up"] * 100, "p_short": (1 - p["p_up"]) * 100, "thr": thr * 100}
    if not side:
        return out
    entry = (LIVE.get(sym) or {}).get("mark") or p["close"]
    R = prob.K_ATR * p["atr"]
    stop, target = entry - side * R, entry + side * R
    q = LIVE.get(sym) or {}
    cost = stats.costs_pct(SETTINGS, q.get("spread_bps") or SYMBOLS[sym]["spread_bps"], fee_pct(sym, SETTINGS))
    notional, margin, lev = stats.position_size(SETTINGS["capital"], entry, stop, max_lev(sym, SETTINGS), SETTINGS,
                                                cost, "risk")
    out.update(entry=entry, stop=stop, target=target, notional=notional, margin=margin, lev=lev,
               liq=stats.liq_price(entry, side, lev, SETTINGS), cost_r=cost / (R / entry),
               risk_usdc=SETTINGS["capital"] * SETTINGS["risk_pct"] / 100)
    return out


def _collect_alerts() -> None:
    seen = {a["id"] for a in ALERTS}
    for (sym, tf), st in STATE.items():
        if tf not in VIEW_TFS:
            continue
        last = len(st["bars"]) - 1
        ts = st["bars"]["ts"].to_numpy()
        for ev in st["res"]["events"]:
            if ev["i"] < last - 1 or (ev["type"] == "fvg" and ev["score"] < ICT_DEFAULTS.get("alert_conf", 3)):
                continue
            _push(seen, sym, tf, ev["type"], ev["dir"], ev["text"], int(ts[ev["i"]]))
        sg = _signal(sym, tf)
        if sg.get("side"):
            _push(seen, sym, tf, "signal", sg["side"],
                  f"{'LONG' if sg['side'] == 1 else 'SHORT'} {max(sg['p_long'], sg['p_short']):.0f}%", int(ts[last]))
    del ALERTS[:-100]


def _push(seen, sym, tf, kind, d, text, ts):
    aid = f"{sym}-{tf}-{kind}-{d}-{ts}"
    if aid not in seen:
        ALERTS.append({"id": aid, "symbol": sym, "tf": tf, "type": kind, "dir": d, "text": text, "ts": ts})
        seen.add(aid)


# ── loops ──────────────────────────────────────────────────────────────────
async def broadcast(msg: dict) -> None:
    for ws in list(CLIENTS):
        try:
            await ws.send_json(msg)
        except Exception:
            CLIENTS.discard(ws)


async def omni_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                LIVE.update(await data.fetch_omni(client))
                META["last_omni"], META["omni_error"] = time.time(), None
                await asyncio.to_thread(data.save_omni_ticks, LIVE, int(time.time()))
                await broadcast({"type": "live", "live": LIVE, "session_open": data.session_open(), "meta": META})
            except Exception as e:
                META["omni_error"] = str(e)[:200]
                log.warning("omni: %s", e)
            await asyncio.sleep(5)


async def oracle_loop():
    """Runs alongside the candle feed: CoinGecko consensus every 2 min, venue index every 30 s."""
    async with httpx.AsyncClient() as client:
        last_cg = 0.0
        while True:
            try:
                venue = {}
                for sym in SYMBOLS:
                    try:
                        venue[sym] = await data.fetch_venue_index(client, sym)
                    except Exception as e:
                        log.warning("venue index %s: %s", sym, e)
                cg = {}
                if time.time() - last_cg >= ORACLE_EVERY_S:
                    cg = await data.fetch_coingecko_consensus(client)
                    last_cg = time.time()
                for sym in SYMBOLS:
                    o = ORACLE.setdefault(sym, {})
                    if venue.get(sym):
                        o["venue"] = {**venue[sym], "ts": int(time.time())}
                    if sym in cg:
                        o["consensus"] = {k: v for k, v in cg[sym].items()}
                        await asyncio.to_thread(data.save_oracle, sym, cg[sym], venue.get(sym))
                META["last_oracle"], META["oracle_error"] = time.time(), None
                await broadcast({"type": "oracle", "oracle": ORACLE})
            except Exception as e:
                META["oracle_error"] = str(e)[:200]
                log.warning("oracle: %s", e)
            await asyncio.sleep(30)


async def candle_loop():
    """Every 5 s, 24/7: latest candles for every market and timeframe from Binance / Hyperliquid.
    The forming candle goes straight to the charts; a newly closed bar triggers a recompute."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                updates = {}
                jobs = [(sym, tf) for sym in SYMBOLS for tf in CALC_TFS]
                results = await asyncio.gather(*(data.fetch_latest(client, s_, t_) for s_, t_ in jobs),
                                               return_exceptions=True)
                for (sym, tf), rows in zip(jobs, results):
                    if isinstance(rows, Exception):
                        META["candle_error"] = f"{sym} {tf}: {rows}"[:200]
                        continue
                    if tf in VIEW_TFS and rows:
                        updates.setdefault(sym, {})[tf] = [_row_candle(r) for r in rows]
                META["last_candles"] = time.time()
                if updates:
                    META["candle_error"] = None
                await broadcast({"type": "candles", "candles": updates, "meta": META})
                if _closed_changed() and not _recalc_lock.locked():
                    asyncio.create_task(_recalc())
            except Exception as e:
                META["candle_error"] = str(e)[:200]
                log.warning("candles: %s", e)
            await asyncio.sleep(5)


async def _recalc():
    async with _recalc_lock:
        await asyncio.to_thread(recompute)
        await broadcast({"type": "recalc", "version": META["version"]})


def _row_candle(r) -> dict:
    ts, o, h, l, c, v, tb = r
    return {"time": _et(ts), "open": o, "high": h, "low": l, "close": c, "volume": v,
            "buy_pct": (tb / v * 100) if (tb is not None and v) else None}


async def loris_loop():
    """Loris Tools funding rates for every venue, every LORIS_POLL_SEC (default 60 s)."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                parsed = loris.parse(await loris.fetch(client))
                LORIS.update(parsed)
                await asyncio.to_thread(loris.save, parsed)
                META["last_loris"], META["loris_error"] = time.time(), None
                if not any(d["venues"] for d in parsed.values()):
                    META["loris_error"] = ("No NVDA/AMD funding in the Loris response: the free plan returns BTC/ETH only"
                                           if not loris.api_key() else "None of the dashboard symbols are in the Loris response")
                await broadcast({"type": "loris", "loris": LORIS, "meta": META})
            except Exception as e:
                META["loris_error"] = str(e)[:200]
                log.warning("loris: %s", e)
            await asyncio.sleep(loris.poll_sec())


async def social_loop():
    """X (Twitter) tweets about the AMD / NVDA perps, every X_POLL_SEC (default 5 min), when a key is in .env."""
    async with httpx.AsyncClient() as client:
        while True:
            if social.provider():
                try:
                    new = await social.poll(client)
                    META["last_social"], META["social_error"] = time.time(), None
                    seen = {a["id"] for a in ALERTS}
                    for sym in new:
                        s = social.summary(sym)
                        if s and s["buzz"] and s["buzz"] >= 3 and s["h1"]["n"] >= 10:
                            snt = s["h1"]["sent"] or 0
                            _push(seen, sym, "X", "social", 1 if snt >= 0 else -1,
                                  f"X buzz {s['buzz']:.1f}× ({s['h1']['n']} tweets/h) · sentiment {snt:+.2f}",
                                  int(time.time()) // 3600 * 3600)
                    if any(new.values()):
                        await broadcast({"type": "social", "social": {s_: social.summary(s_) for s_ in new}, "meta": META})
                except Exception as e:
                    META["social_error"] = str(e)[:200]
                    log.warning("social: %s", e)
            await asyncio.sleep(social.poll_sec())


def _refresh_all(full: bool):
    errors = {}
    for sym in SYMBOLS:
        e = data.refresh_history(sym, full=full)
        if e:
            errors[sym] = e[0][:200]
    META["history_error"] = errors
    META["last_candles"] = time.time()


def _closed_changed() -> bool:
    changed = False
    for sym in SYMBOLS:
        for tf in VIEW_TFS:
            b = data.load_bars(sym, tf)
            b = b[b["closed"]]
            key = int(b["ts"].iloc[-1]) if len(b) else 0
            if _last_closed.get((sym, tf)) != key:
                _last_closed[(sym, tf)] = key
                changed = True
    return changed


@asynccontextmanager
async def lifespan(app):
    full = not all(data.has_history(s) for s in SYMBOLS)
    if full:
        # First run on this computer: the page opens right away and fills in when the download finishes.
        META["loading"] = True
        log.info("first start: downloading history in the background (a few minutes)")

        async def first_backfill():
            await asyncio.to_thread(_refresh_all, True)
            META["loading"] = False
            _closed_changed()
            await _recalc()
        backfill = asyncio.create_task(first_backfill())
    else:
        await asyncio.to_thread(_refresh_all, False)
    try:
        async with httpx.AsyncClient() as client:
            LIVE.update(await data.fetch_omni(client))
    except Exception as e:
        log.warning("omni at startup: %s", e)
    _closed_changed()
    await asyncio.to_thread(recompute)
    tasks = [asyncio.create_task(omni_loop()), asyncio.create_task(candle_loop()), asyncio.create_task(oracle_loop()),
             asyncio.create_task(social_loop()), asyncio.create_task(loris_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── payload ────────────────────────────────────────────────────────────────
def _et(ts: int) -> int:
    """Times go to the browser as UTC epoch seconds; the page shifts them to the chosen time zone."""
    return int(ts)


def _rvol(b: pd.DataFrame, forming: pd.DataFrame, tf: str) -> dict:
    v = b["volume"].to_numpy(float)
    avg = float(np.mean(v[-21:-1])) if len(v) > 21 else float(np.mean(v)) if len(v) else 0
    last = float(v[-1]) if len(v) else 0
    out = {"last": last, "avg20": avg, "rvol": last / avg if avg else None}
    tb = b["taker_buy"].to_numpy(float) if "taker_buy" in b else None
    if tb is not None and len(tb) and not np.isnan(tb[-1]) and v[-1]:
        out["buy_pct"] = float(tb[-1] / v[-1] * 100)                       # last closed bar
        w = slice(-12, None)
        out["buy_pct_12"] = float(np.nansum(tb[w]) / np.nansum(v[w]) * 100)  # last 12 bars
    if len(forming):
        f = forming.iloc[-1]
        start, end = int(f["ts"]), data.bar_end(int(f["ts"]), tf)
        frac = min(1.0, (time.time() - start) / max(1, end - start))
        if frac >= 0.3 and avg:                     # too early in the bar → show the last closed bar instead
            out["forming_rvol"] = float(f["volume"]) / frac / avg               # pace-adjusted
    return out


SHOW_BARS = 3000          # candles sent to the browser per timeframe (the engine uses the full window)


def _chart(sym: str, tf: str) -> dict | None:
    st = STATE.get((sym, tf))
    if not st:
        return None
    b, res = st["bars"], st["res"]
    n = len(b)
    off = max(0, n - SHOW_BARS)                 # bar indexes are shifted so 0 = first candle sent
    sh = lambda i: None if i is None else i - off
    def candle(r):
        tb = r.taker_buy
        return {"time": _et(int(r.ts)), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
                "buy_pct": (tb / r.volume * 100) if (tb is not None and tb == tb and r.volume) else None}
    alive = lambda z: z["end"] is None or z["end"] >= off
    fv = [{**{k: z[k] for k in ("id", "bull", "top", "bot", "state", "score", "size_pct", "comps")},
           "left": max(sh(z["left"]), -1), "end": sh(z["end"])} for z in res["fvgs"] if alive(z)]
    obs = [{**{k: z[k] for k in ("id", "bull", "top", "bot", "state")}, "left": max(sh(z["left"]), -1), "end": sh(z["end"])}
           for z in res["obs"] if alive(z)]
    pd_ = dict(res["pd"], x0=max(0, res["pd"]["x0"] - off)) if res["pd"] else None
    return {
        "candles": [candle(r) for r in b.iloc[off:].itertuples()], "forming": [candle(r) for r in st["forming"].itertuples()],
        "fvgs": fv, "obs": obs,
        "sweeps": [dict(x, i=x["i"] - off, **{"from": max(x["from"] - off, 0)}) for x in res["sweeps"] if x["i"] >= off],
        "structure": [dict(x, i=x["i"] - off, **{"from": max(x["from"] - off, 0)}) for x in res["structure"] if x["i"] >= off],
        "ors": [dict(x, start=x["start"] - off, end=x["end"] - off) for x in res["ors"] if x["start"] >= off],
        "pd": pd_, "table": res["table"], "rvol": _rvol(b, st["forming"], tf),
        "prob": st.get("prob"), "signal": _signal(sym, tf),
    }


OMNI_MIN_BARS = 60


def _omni_chart(sym: str, tf: str) -> dict:
    bars = data.load_omni_bars(sym, tf)
    out = {"candles": bars, "native": None}
    closed = bars[:-1]
    if len(closed) >= OMNI_MIN_BARS:
        res = ict.run(pd.DataFrame(closed).rename(columns={"time": "ts"}), tf)
        out["native"] = {
            "fvgs": [{k: z[k] for k in ("id", "bull", "top", "bot", "left", "end", "state", "score", "size_pct", "comps")}
                     for z in res["fvgs"]],
            "obs": [{k: z[k] for k in ("id", "bull", "top", "bot", "left", "end", "state")} for z in res["obs"]],
            "sweeps": res["sweeps"], "structure": res["structure"], "pd": res["pd"], "table": res["table"], "ors": []}
    return out


@app.get("/api/state")
def api_state(symbol: str = "NVDA"):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        return JSONResponse({"error": "unknown symbol"}, 404)
    group = SYMBOLS[symbol]["group"]
    q = LIVE.get(symbol) or {}
    return {"symbol": symbol, "group": group, "tfs": VIEW_TFS,
            "groups": {g: {"label": c["label"], "symbols": c["symbols"]} for g, c in GROUPS.items()},
            "market": {"omni": SYMBOLS[symbol]["omni"], "source": SOURCE_LABEL[SYMBOLS[symbol]["source"]],
                       "ticker": SYMBOLS[symbol]["ticker"], "fee_pct": fee_pct(symbol, SETTINGS),
                       "max_lev": max_lev(symbol, SETTINGS), "spread_bps": q.get("spread_bps") or SYMBOLS[symbol]["spread_bps"]},
            "charts": {tf: _chart(symbol, tf) for tf in VIEW_TFS},
            "oracle": ORACLE.get(symbol, {}),
            "social": social.summary(symbol),
            "loris": LORIS.get(symbol), "loris_hist": loris.history(symbol), "loris_key": bool(loris.api_key()),
            "oracle_hist": [[_et(t), i, v] for t, i, v in data.load_oracle(symbol)],
            "tradingview": TRADINGVIEW_SYMBOL.get(symbol),
            "omni": {tf: _omni_chart(symbol, tf) for tf in VIEW_TFS},
            "model": {"k_atr": prob.K_ATR, "horizon": prob.HORIZON, "report_time": _report().get("generated")},
            "live": LIVE, "alerts": ALERTS[-40:], "settings": SETTINGS, "meta": META,
            "session_open": data.session_open()}


@app.post("/api/settings")
async def set_settings(body: dict):
    for k, v in body.items():
        if k in SETTINGS and k != "horizon" and isinstance(v, (int, float)) and v >= 0:
            SETTINGS[k] = float(v)
    await broadcast({"type": "recalc", "version": META["version"]})
    return SETTINGS


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    CLIENTS.add(websocket)
    try:
        await websocket.send_json({"type": "live", "live": LIVE, "session_open": data.session_open(), "meta": META})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(websocket)


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
