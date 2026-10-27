"""Central settings. Everything the UI can change lives in SETTINGS (a plain dict)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.sqlite"   # Binance / Hyperliquid candles (old Yahoo cache: bars.sqlite)
FRONTEND_DIR = ROOT / "frontend"

# Chart data: free public APIs only. Binance USD-M futures (TradFi perps) first; Hyperliquid (HIP-3 "xyz" dex)
# when Binance has no market. US30 and DXY have no free candle source (Binance: none; Hyperliquid: DXY delisted,
# no Dow; Lighter: candles endpoint returns 403), so they are not charted.
# omni: Variational Omni ticker for the live mark / sizing. kind: Omni funding style ("perp" | "swap").
SYMBOLS = {
    "AMD":   {"source": "binance",     "ticker": "AMDUSDT",  "hl_ticker": "xyz:AMD",    "omni": "AMD-PERP",   "max_lev": 20, "group": "semis", "kind": "perp", "spread_bps": 5.0},
    "NVDA":  {"source": "binance",     "ticker": "NVDAUSDT",  "hl_ticker": "xyz:NVDA",   "omni": "NVDA-PERP",  "max_lev": 20, "group": "semis", "kind": "perp", "spread_bps": 5.0},
    "SOXL":  {"source": "binance",     "ticker": "SOXLUSDT",  "hl_ticker": "xyz:SOXL",   "omni": "SOXL-PERP",  "max_lev": 15, "group": "semis", "kind": "perp", "spread_bps": 5.0},
    "US100": {"source": "hyperliquid", "ticker": "xyz:XYZ100", "omni": "US100-SWAP", "max_lev": 20, "group": "macro", "kind": "swap", "spread_bps": 2.0},
}
SOURCE_LABEL = {"binance": "Binance", "hyperliquid": "Hyperliquid"}


def use_hyperliquid_for_binance() -> list[str]:
    """Where Binance futures are blocked (HTTP 451 / 403, e.g. US servers), chart the same perps from
    Hyperliquid's xyz dex instead (xyz:NVDA, xyz:AMD, xyz:SOXL). Returns the symbols switched."""
    moved = []
    for sym, m in SYMBOLS.items():
        if m["source"] == "binance" and m.get("hl_ticker"):
            m.update(source="hyperliquid", ticker=m["hl_ticker"])
            moved.append(sym)
    for g in GROUPS.values():
        g["label"] = g["label"].replace("Binance", "Hyperliquid")
    return moved

# Parallel price check: CoinGecko /derivatives (free, no key) lists the perps of ~20 venues with each venue's
# index (oracle) price. Consensus = median index across venues whose ticker matches these base names.
COINGECKO_DERIVATIVES = "https://api.coingecko.com/api/v3/derivatives"
ORACLE_ALIASES = {"NVDA": ["NVDA"], "AMD": ["AMD"], "SOXL": ["SOXL"],
                  "US100": ["XYZ100", "US100", "NAS100", "NDX100", "USTECH100"]}
ORACLE_EVERY_S = 120          # the CoinGecko payload is ~9 MB, so not more often than every 2 minutes
# TradingView embeddable widget symbols (reference view only — TradingView has no data API).
TRADINGVIEW_SYMBOL = {"NVDA": "BINANCE:NVDAUSDT.P", "AMD": "BINANCE:AMDUSDT.P", "SOXL": "BINANCE:SOXLUSDT.P",
                      "US100": "PEPPERSTONE:NAS100"}

# X (Twitter) searches per symbol (backend/social.py). Cashtag, the Binance perp ticker, Omni's market name and
# "perp" phrases; retweets are dropped by the provider filter. Edit freely (X API limit: 512 characters).
SOCIAL_QUERIES = {
    "AMD":  '($AMD OR AMDUSDT OR "AMD perp" OR "AMD perps" OR "AMD-PERP" OR "AMD perpetual" OR "AMD futures")',
    "NVDA": '($NVDA OR NVDAUSDT OR "NVDA perp" OR "NVDA perps" OR "NVDA-PERP" OR "NVDA perpetual" OR "NVDA futures" '
            'OR "Nvidia perp")',
}

GROUPS = {
    "semis": {"label": "Semis · Binance", "symbols": ["AMD", "NVDA", "SOXL"], "pair": ("AMD", "NVDA"), "hedge": "SOXL",
              "sector": ["AMD", "NVDA", "SOXL"], "fee_key": "fee_pct"},
    "macro": {"label": "Index · Hyperliquid", "symbols": ["US100"], "pair": ("US100", "US100"), "hedge": "US100",
              "sector": ["US100"], "fee_key": "swap_fee_pct"},
}

TIMEFRAMES = ["5m", "15m", "1h", "4h"]
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
# Bars kept in memory per timeframe (newest), so a recompute stays a few seconds.
COMPUTE_BARS = {"5m": 20000, "15m": 10000, "1h": 6000, "4h": 3000}
BINANCE_FAPI = "https://fapi.binance.com"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"

OMNI_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"

# Indicator inputs: same names and defaults as the Pine script.
ICT_DEFAULTS = {
    "size_mode": "ATR",        # "ATR" | "% fijo"
    "atr_len": 14,
    "min_atr": 0.30,
    "strong_atr": 0.75,
    "min_pct": 0.05,
    "strong_pct": 0.15,
    "mit_mode": "50% (CE)",    # "Toque" | "50% (CE)" | "Completo (mecha)" | "Completo (cierre)"
    "max_active": 40,
    "swing_len": 5,
    "conf_look": 10,
    "ob_lookback": 5,
    "ob_body": False,
    "pd_len": 100,
    "or_minutes": 15,
    "or_extend": 40,
}

SETTINGS = {
    "capital": 90.0,            # USDC
    "risk_pct": 2.0,            # % of equity lost if the stop is hit (fees included) — position sizing
    "margin_pct": 57.5,         # cap: margin of a trade / of the whole combo never exceeds this % of equity
    "signal_prob": 60.0,        # % → actionable signal
    "watch_prob": 55.0,         # % → watch-list signal
    "rr": 2.0,                  # target in R
    "horizon": {"5m": 12, "1h": 8, "4h": 10},   # bars to fill + bars to resolve
    "fee_pct": 0.05,            # perps: per side, on notional
    "swap_fee_pct": 0.01,       # swaps: per side, on notional
    "swap_max_lev": 20,         # Omni does not publish a swap leverage cap; assumed
    "funding": 1,               # 1 = charge funding in the backtest (current Omni rate held constant)
    "shorts_receive": 1,        # swaps: 1 = longs pay the long rate, shorts receive the short rate (Omni docs:
                                # "typically longs pay and shorts receive"); 0 = shorts pay their rate too
    "slippage_pct": 0.02,       # per side
    "mmr_pct": 0.5,             # assumed maintenance margin (Omni does not publish one per market)
    "liq_buffer": 0.8,          # stop must sit within 80% of the distance to liquidation
    "stop_atr_buffer": 0.10,    # stop = distal FVG edge ± 0.1 ATR
    # Filters validated out of sample (research/filter_search.py): applied to the "Filtered" backtest row
    # and flagged on live setups.
    "max_cost_r": 0.25,         # skip setups whose round-trip costs exceed this many R (0 = off)
    "late_only_1h": 1,          # 1h: only setups whose bar closes at/after 14:00 ET (1 = on)
    "min_samples": 20,          # resolved setups needed before a bucket's own win-rate is trusted
    "beta_lookback_bars": 390,  # 1h bars (~60 sessions) for AMD/NVDA vs SOXL beta
}
