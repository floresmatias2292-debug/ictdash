"""X (Twitter) feed for the perps: tweets about AMD / NVDA (cashtags, Binance AMDUSDT/NVDAUSDT, Omni AMD-PERP ...),
stored in SQLite, scored bullish / bearish with a small EN + ES word list, and summarized per symbol
(buzz vs the 24 h pace, sentiment, perp mentions, top and latest tweets).

Two providers, picked by the key found in .env (project root) or the environment:
  TWITTERAPI_IO_KEY   twitterapi.io  — third-party, ~$0.15 per 1,000 tweets (cheapest)
  X_BEARER_TOKEN      official X API v2 recent search — pay-per-use, ~$0.005 per tweet read
Optional: X_PROVIDER (twitterapi | x), X_POLL_SEC (default 300), X_DAILY_CAP (tweets per UTC day).

Check a key from the project folder:  .venv/bin/python -m backend.social
"""
from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import DB_PATH, ROOT, SOCIAL_QUERIES

log = logging.getLogger("social")

try:                                    # .env next to run.sh, never committed (see .gitignore)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

X_SEARCH = "https://api.x.com/2/tweets/search/recent"
TWAPI_SEARCH = "https://api.twitterapi.io/twitter/tweet/advanced_search"
PRICE_PER_1K = {"twitterapi": 0.15, "x": 5.0}          # USD per 1,000 tweets returned (published rates, 2026)
DEFAULT_CAP = {"twitterapi": 3000, "x": 200}            # tweets per UTC day ≈ $0.45 / $1.00


def provider() -> str | None:
    want = os.environ.get("X_PROVIDER", "").strip().lower()
    have = {"twitterapi": bool(os.environ.get("TWITTERAPI_IO_KEY")), "x": bool(os.environ.get("X_BEARER_TOKEN"))}
    if want in have:
        return want if have[want] else None
    return "twitterapi" if have["twitterapi"] else "x" if have["x"] else None


def poll_sec() -> int:
    return max(60, int(os.environ.get("X_POLL_SEC", "300")))


def daily_cap(p: str) -> int:
    return int(os.environ.get("X_DAILY_CAP", DEFAULT_CAP[p]))


# ── sentiment: small word list, English + Spanish, trader slang ─────────────
BULL = ["bull", "bullish", "long", "longs", "longed", "longing", "calls", "call", "buy", "buying", "bought", "breakout",
        "rip", "ripping", "moon", "pump", "squeeze", "higher", "upside", "support held", "bounce", "ath", "beat", "beats",
        "upgrade", "strong", "accumulate", "send it", "green", "alcista", "compra", "comprando", "subida", "sube",
        "rebote", "largo", "romper"]
BEAR = ["bear", "bearish", "short", "shorts", "shorted", "shorting", "puts", "put", "sell", "selling", "sold", "dump",
        "dumping", "crash", "breakdown", "lower", "downside", "rejected", "rejection", "overvalued", "bubble", "miss",
        "missed", "downgrade", "weak", "red", "liquidated", "rekt", "bajista", "venta", "vendiendo", "baja", "cae",
        "caida", "caída", "corto", "burbuja"]
BULL_EMOJI, BEAR_EMOJI = "🚀📈🟢🐂💚🔥", "📉🔴🐻🩸💀"
PERP_RE = re.compile(r"\b(perps?|perpetual|usdt|amdusdt|nvdausdt|[a-z]{3,4}-perp|funding|leverage|apalancamiento|"
                     r"liquidat\w*|x\d{1,3}|\d{1,3}x|binance|hyperliquid|omni|variational|bybit|bitget|okx)\b", re.I)


def _word_re(words):
    return re.compile(r"(?<![\w$])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?!\w)", re.I)


_BULL_RE, _BEAR_RE = _word_re(BULL), _word_re(BEAR)
_NEG_RE = re.compile(r"\b(not|no|never|don'?t|isn'?t|nunca|sin)\s+(\w+)", re.I)


def sentiment(text: str) -> float:
    """-1 (bearish) … +1 (bullish); 0 = neutral or mixed. Negation flips the next word ("not bullish")."""
    t = re.sub(r"https?://\S+", " ", text)
    negated = {m.group(2).lower() for m in _NEG_RE.finditer(t)}
    b = sum(-1 if m.group(1).lower() in negated else 1 for m in _BULL_RE.finditer(t))
    s = sum(-1 if m.group(1).lower() in negated else 1 for m in _BEAR_RE.finditer(t))
    b += sum(t.count(e) for e in BULL_EMOJI)
    s += sum(t.count(e) for e in BEAR_EMOJI)
    return 0.0 if b == s else max(-1.0, min(1.0, (b - s) / max(b + s, 1)))


# ── storage ────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS tweets (id TEXT, symbol TEXT, ts INTEGER, user TEXT, name TEXT,
                   followers INTEGER, verified INTEGER, text TEXT, url TEXT, likes INTEGER, rts INTEGER, replies INTEGER,
                   views INTEGER, sent REAL, perp INTEGER, PRIMARY KEY (id, symbol))""")
    con.execute("CREATE INDEX IF NOT EXISTS tweets_sym_ts ON tweets (symbol, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS social_usage (day TEXT, provider TEXT, tweets INTEGER, calls INTEGER, "
                "PRIMARY KEY (day, provider))")
    return con


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def usage(p: str) -> dict:
    with _db() as con:
        r = con.execute("SELECT tweets, calls FROM social_usage WHERE day=? AND provider=?", (_today(), p)).fetchone()
    n, calls = r or (0, 0)
    return {"tweets": n, "calls": calls, "cap": daily_cap(p), "usd": n / 1000 * PRICE_PER_1K[p]}


def _add_usage(p: str, n: int) -> None:
    with _db() as con:
        con.execute("INSERT INTO social_usage VALUES (?,?,?,1) ON CONFLICT(day, provider) DO UPDATE SET "
                    "tweets = tweets + excluded.tweets, calls = calls + 1", (_today(), p, n))


def _newest(symbol: str) -> tuple[str | None, int | None]:
    with _db() as con:
        r = con.execute("SELECT id, ts FROM tweets WHERE symbol=? ORDER BY ts DESC, length(id) DESC, id DESC LIMIT 1",
                        (symbol,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _save(symbol: str, rows: list[dict]) -> int:
    with _db() as con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO tweets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(r["id"], symbol, r["ts"], r["user"], r["name"], r["followers"], int(r["verified"]), r["text"],
                          r["url"], r["likes"], r["rts"], r["replies"], r["views"], sentiment(r["text"]),
                          int(bool(PERP_RE.search(r["text"])))) for r in rows])
        return con.total_changes - before


# ── providers ──────────────────────────────────────────────────────────────
def _ts(v) -> int:
    if not v:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())       # X API: ISO 8601
    except ValueError:
        return int(parsedate_to_datetime(str(v)).timestamp())                              # "Tue Dec 10 07:00:30 +0000 2024"


async def _fetch_x(client: httpx.AsyncClient, query: str, since_id: str | None, since_ts: int | None) -> list[dict]:
    params = {"query": f"{query} -is:retweet", "max_results": 100 if since_id else 25, "sort_order": "recency",
              "tweet.fields": "created_at,public_metrics,lang,author_id", "expansions": "author_id",
              "user.fields": "username,name,verified,verified_type,public_metrics"}
    if since_id:
        params["since_id"] = since_id
    r = await client.get(X_SEARCH, params=params, timeout=20,
                         headers={"Authorization": f"Bearer {os.environ['X_BEARER_TOKEN']}"})
    r.raise_for_status()
    j = r.json()
    users = {u["id"]: u for u in j.get("includes", {}).get("users", [])}
    out = []
    for t in j.get("data", []):
        u, m = users.get(t.get("author_id"), {}), t.get("public_metrics", {})
        user = u.get("username", "")
        out.append({"id": t["id"], "ts": _ts(t.get("created_at")), "user": user, "name": u.get("name", ""),
                    "followers": (u.get("public_metrics") or {}).get("followers_count", 0),
                    "verified": bool(u.get("verified")) or u.get("verified_type") in ("blue", "business", "government"),
                    "text": t.get("text", ""), "url": f"https://x.com/{user or 'i'}/status/{t['id']}",
                    "likes": m.get("like_count", 0), "rts": m.get("retweet_count", 0) + m.get("quote_count", 0),
                    "replies": m.get("reply_count", 0), "views": m.get("impression_count", 0)})
    return out


async def _fetch_twitterapi(client: httpx.AsyncClient, query: str, since_id: str | None, since_ts: int | None) -> list[dict]:
    # Billed per tweet returned and there is no since_id, so ask only for tweets newer than the last one stored.
    q = f"{query} -filter:retweets"
    if since_ts and os.environ.get("X_SINCE_TIME", "1") != "0":
        q += f" since_time:{since_ts}"
    r = await client.get(TWAPI_SEARCH, params={"query": q, "queryType": "Latest"},
                         headers={"X-API-Key": os.environ["TWITTERAPI_IO_KEY"]}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("status") == "error":
        raise RuntimeError(j.get("msg") or j.get("message") or "twitterapi.io error")
    out = []
    for t in j.get("tweets", []):
        a = t.get("author") or {}
        user = a.get("userName") or a.get("screen_name") or ""
        out.append({"id": str(t["id"]), "ts": _ts(t.get("createdAt")), "user": user, "name": a.get("name", ""),
                    "followers": a.get("followers") or a.get("followersCount") or 0,
                    "verified": bool(a.get("isBlueVerified") or a.get("isVerified")),
                    "text": t.get("text", ""), "url": t.get("url") or f"https://x.com/{user or 'i'}/status/{t['id']}",
                    "likes": t.get("likeCount", 0), "rts": (t.get("retweetCount") or 0) + (t.get("quoteCount") or 0),
                    "replies": t.get("replyCount", 0), "views": t.get("viewCount") or 0})
    return out


async def poll(client: httpx.AsyncClient) -> dict:
    """One read per symbol (newest page). Returns {symbol: new tweets stored}. Stops at the daily cap."""
    p = provider()
    if not p:
        return {}
    fetch = _fetch_twitterapi if p == "twitterapi" else _fetch_x
    out = {}
    for sym, q in SOCIAL_QUERIES.items():
        if usage(p)["tweets"] >= daily_cap(p):
            raise RuntimeError(f"daily cap reached ({daily_cap(p)} tweets, X_DAILY_CAP); resumes at 00:00 UTC")
        rows = await fetch(client, q, *_newest(sym))
        _add_usage(p, len(rows))
        out[sym] = _save(sym, rows)
    return out


# ── summary for the dashboard ──────────────────────────────────────────────
def _weight(followers, likes, rts) -> float:
    return 1 + math.log10(1 + (followers or 0)) + math.log10(1 + (likes or 0) + 2 * (rts or 0))


def summary(symbol: str, now: float | None = None) -> dict | None:
    if symbol not in SOCIAL_QUERIES:
        return None
    now = int(now or time.time())
    with _db() as con:
        rows = con.execute("SELECT id, ts, user, name, followers, verified, text, url, likes, rts, replies, views, sent, "
                           "perp FROM tweets WHERE symbol=? AND ts>=? ORDER BY ts DESC", (symbol, now - 48 * 3600)).fetchall()
    keys = ["id", "ts", "user", "name", "followers", "verified", "text", "url", "likes", "rts", "replies", "views",
            "sent", "perp"]
    tw = [dict(zip(keys, r)) for r in rows]
    d1 = [t for t in tw if t["ts"] >= now - 3600]
    d24 = [t for t in tw if t["ts"] >= now - 86400]

    def agg(ts):
        w = [_weight(t["followers"], t["likes"], t["rts"]) for t in ts]
        sw = sum(w)
        return {"n": len(ts), "sent": (sum(t["sent"] * x for t, x in zip(ts, w)) / sw) if sw else None,
                "bull": sum(t["sent"] > 0 for t in ts), "bear": sum(t["sent"] < 0 for t in ts),
                "perp": sum(t["perp"] for t in ts)}

    pace = len(d24) / 24 if d24 else 0
    hourly = [0] * 48                                          # tweets per hour, oldest first
    for t in tw:
        k = (now - t["ts"]) // 3600
        if 0 <= k < 48:
            hourly[47 - k] += 1
    top = sorted(d24, key=lambda t: (t["likes"] or 0) + 2 * (t["rts"] or 0) + (t["replies"] or 0), reverse=True)[:5]
    p = provider()
    return {"symbol": symbol, "query": SOCIAL_QUERIES[symbol], "h1": agg(d1), "h24": agg(d24),
            "buzz": (len(d1) / pace) if len(d24) >= 24 else None,     # needs ~1 tweet/h of history to mean anything "hourly": hourly,
            "latest": tw[:25], "top": top, "provider": p, "usage": usage(p) if p else None, "poll_sec": poll_sec()}


# ── key check: .venv/bin/python -m backend.social ───────────────────────────
if __name__ == "__main__":
    import asyncio

    async def _check():
        p = provider()
        if not p:
            print("No key found. Put TWITTERAPI_IO_KEY=... or X_BEARER_TOKEN=... in", ROOT / ".env")
            return 1
        print(f"provider: {p}")
        async with httpx.AsyncClient() as c:
            n = await poll(c)
        print("new tweets stored:", n)
        for sym in SOCIAL_QUERIES:
            s = summary(sym)
            print(f"\n{sym}: {s['h24']['n']} tweets in 24 h · sentiment {s['h24']['sent']}")
            for t in s["latest"][:3]:
                print(f"  @{t['user']}: {t['text'][:110]!r}")
        print("\nusage today:", usage(p))
        return 0

    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_check()))
