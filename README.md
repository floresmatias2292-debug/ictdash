# FVG-ICT Charts

TradingView-style charts on 5m / 15m / 1h, 24/7 perp prices from free public APIs:
Binance USD-M futures for AMD, NVDA, SOXL (AMDUSDT, NVDAUSDT, SOXLUSDT) and Hyperliquid's HIP-3 market
xyz:XYZ100 for US100. US30 and DXY are not charted: no free source has candles for them (Binance lists neither,
Hyperliquid's xyz:DXY is delisted and there is no Dow, Lighter's candle endpoint returns 403).
Candles refresh every 5 s around the clock; Variational Omni supplies only the live mark line.

Parallel oracle check (runs alongside the candle feed): every 30 s the venue's own index price (Binance
`premiumIndex`, Hyperliquid `oraclePx`), and every 2 min a CoinGecko consensus — the median index (oracle) price of
every perp venue CoinGecko's free `/derivatives` endpoint lists for the market (~40 venues for NVDA). Drawn as the
purple "CG" line and compared in the "Oracle check" panel. The CoinGecko payload is ~9 MB per read (~280 MB/hour).
TradingView has no oracle or data API; the "TradingView" source button shows its free embed widget
(BINANCE:NVDAUSDT.P etc., PEPPERSTONE:NAS100 for US100) as a visual reference only.
Drawn by a bar-by-bar Python port of the Pine indicator "FVG ICT — Confluencias (Sweep · MSS · OB · P/D · OR)".
Volume, relative volume and Binance taker-buy share under every chart; layouts (1 large + 2, 3 columns, 3 rows,
single) and maximize.

Long/short probability (`backend/prob.py`): P(price hits +1×ATR before −1×ATR from the next bar, within
12 / 8 / 8 bars on 5m / 15m / 1h), from a logistic model on the indicator's per-bar state. Validated walk-forward in
`research/prob_validate.py`: out of sample the AUC is ≈ 0.45–0.51 on every market and timeframe, i.e. the indicator
state carries no directional edge, so the model is heavily regularized and keeps probabilities near 50%. The dashboard
shows that skill next to every probability; a signal appears only if a probability crosses the threshold (60%).

## MET (Meteora) stake hedge

- **Chart:** Hyperliquid MET perp (main dex, 1h history since Apr 2026, 4h since the Oct 2025 listing) with
  the FVG-ICT engine; Omni METEORA-PERP gives the live mark, funding and the hedge price.
- **Panel "MET stake hedge"** (Settings ⚙: `MET staked (tokens)`, `hedge size %`, `leverage` ≤ x3):
  1. *Static hedge*: short the staked amount all the time → price exposure ≈ 0, you keep the staking yield.
     Shows the order, notional, margin, liquidation price of the short and Omni funding per day.
  2. *4h hedge*: short only while the 4h structure is bearish; an alert + double bell when it turns.
- **Backtest** (`research/met_hedge.py`, 1 staked MET, net of fee + spread + slippage per switch and the real
  hourly Hyperliquid funding; staking yield not modelled):

  | 4h bars, Oct 2025 – Nov 2026 | Final | Worst drop | Switches |
  |---|---|---|---|
  | Stake only | −83% | −94% | 0 |
  | Static hedge | +0.6% | −0.8% | 1 |
  | 4h-structure hedge | −25% | −38% | 56 |

  On 1h bars (Apr – Nov 2026, MET +103%) the 1h-structure hedge lost (−10%, 120 switches, worst drop −71%),
  so the 1h / 5m readings are shown only as early warnings. One token and one year: the 4h hedge is a
  trade-off, not a proven edge.
- If MET rises the short loses what the locked stake gains: keep spare USDC for margin. No orders are placed.

## Big-move bells

The forming 5m or 1h candle body reaching `big_move_atr` × ATR (Settings, default 2, 0 = off) raises an
alert on any market: **one bell** for a big LONG move, **two bells** for a big SHORT move. The MET hedge
turning on / off rings two / one bell too. Turn sound on with 🔔 in the toolbar; test with the buttons in
the Alerts panel.

## US100 / US30 pair (delta scalping check)

- Omni US100-SWAP / US30-SWAP, beta-hedged. No free source has US30 candles, so the live
  spread is built from the Omni marks (5 s ticks → 5m bars). It shows "collecting" until 106 bars of a
  moving market have been recorded without gaps.
- Validated on the old Yahoo cache (`data/bars.sqlite`, US cash session), walk-forward, net of costs:
  - 5m, fading the spread: 37 trades, −0.17% per trade (95% range below zero, so it loses).
  - 5m, following it: 29 trades, +0.15% per trade, but the range includes zero.
  - 1h over 3 years: fading +0.55% per trade (21 trades, range includes zero); following −0.16%.

  Scalping the pair on 5m does not show an edge: costs on both legs (≈0.13% round trip) eat the small moves.

  ```bash
  .venv/bin/python -W ignore research/pair_validate.py revert index
  .venv/bin/python -W ignore research/pair_validate.py revert index 1h
  ```

## Paper trading

LONG / SHORT buttons on the large futures chart open a fictitious trade at the futures price, always x15 with
12 USDC margin (180 USDC notional). The panel shows open P&L (after fees), a Close button and the history. A
trade is liquidated, and its margin lost, when the price touches the orange liquidation line. Funding is not
charged. Change the fixed size in `PAPER` in `backend/config.py`.

## Install on another computer

```bash
git clone https://github.com/floresmatias2292-debug/ictdash.git ~/dashboard/ictdash
bash ~/dashboard/ictdash/deploy/install.sh
```

Needs Python 3.11+ with `python3-venv`. The script creates `.venv`, installs `requirements.txt` and starts the
dashboard as a user service at http://localhost:8050. The first start downloads the price history from Binance
and Hyperliquid. To bring paper trades and the collected Omni prices along, copy `data/market.sqlite` from the
old computer before running the script.

## US100 lead (semis and US100)

`backend/lead.py`, tested in `research/us100_lead.py` (Yahoo cache, US cash session, walk-forward, net of costs):

| Idea | Result |
|---|---|
| When a big US100 move comes (≥3×ATR in 12 × 5m / ≥1.5×ATR in 3 × 1h) | validated: AUC 0.72 / 0.63; top 20% of readings 88% / 69% vs 27% / 35% |
| US100 direction (with US30) | coin flip (AUC 0.52) |
| Weekend: 24/7 perp move since the cash close vs Monday's gap | same sign on 27 of 27 weekends (correlation 0.92) |
| Monday gap during the day | reverses more than it continues (correlation −0.30); fading big gaps on SOXL +1.24%/trade, range includes zero |
| Trade SOXL the way US100 just moved | loses: −0.49%/trade (1h), −0.48% (5m). SOXL moves in the same bar (β ≈ 5–6), not after |
| AMD / NVDA pair (x10, 15 USDC per leg), long the higher β when US100 is bullish | loses: −0.33%/trade (1h), negative even before costs |

The panel shows the big-move meter, the expected gap while the cash market is shut, the SOXL and pair readings
with these results, and buttons to paper-trade the pair. Settings: `LEAD` in `backend/config.py`.

## Run

Needs **Python 3.12+** (numpy 2.5 / pandas 3.1). On any computer (Linux, macOS, Windows):

```bash
git clone https://github.com/floresmatias2292-debug/ictdash.git && cd ictdash
./run.sh              # Linux/macOS   (Windows: double-click run.bat, or  py start.py)
```

`start.py` creates `.venv` on first run, rebuilds it if it was copied from another computer, reinstalls
dependencies when `requirements.txt` changes, then serves http://localhost:8050. Do not copy `.venv` or `data/`
between computers; `data/market.sqlite` is backfilled from Binance / Hyperliquid on the first start.

Permanent on Linux (systemd user service for whatever folder the project is in):

```bash
deploy/install-service.sh                          # http://localhost:8050
systemctl --user status ictdash.service           # state
journalctl --user -u ictdash.service -f           # live logs
systemctl --user restart ictdash.service          # after code changes
```

Where Binance futures are blocked (HTTP 451, e.g. US servers) the dashboard charts AMD / NVDA / SOXL from
Hyperliquid xyz (xyz:AMD, xyz:NVDA, xyz:SOXL) automatically; force it with `CHART_SOURCE=hyperliquid` or `binance` in `.env`.

If it fails: `python3 --version` must be 3.12+; on Ubuntu/Debian `sudo apt install python3.12 python3.12-venv`;
port in use → `PORT=8060 ./run.sh`.

## Funding by venue (Loris Tools)

Side panel "Funding · Loris Tools": the live funding rate of every venue listing the perp (Omni/Variational, Binance,
Hyperliquid xyz, Lighter, Ostium …) from [loris.tools](https://loris.tools) `GET /funding`, 8 h-normalized bps and APR,
the cross-venue median, the cheapest venue to be long / best to be short and their spread, plus a 72 h history line
built from our own reads (table `loris_funding`). Read every `LORIS_POLL_SEC` (60 s).

Setup: `LORIS_API_KEY=...` in `.env`, restart. The free plan returns BTC and ETH only, so NVDA / AMD need a paid plan.
Check the key and the symbol / venue names: `.venv/bin/python -m backend.loris` (raw reply in `data/loris_last.json`).

## X (Twitter) feed for AMD / NVDA perps

Side panel "X · Twitter": tweets about the perp ($AMD / $NVDA cashtags, Binance AMDUSDT / NVDAUSDT, Omni AMD-PERP /
NVDA-PERP, "perp" phrases), buzz (tweets in the last hour vs the 24 h pace), bullish / bearish sentiment (EN + ES word
list weighted by followers and engagement), share mentioning perps, top and latest tweets. An alert fires when buzz
reaches 3× with ≥ 10 tweets/h. Tweets are kept in `data/market.sqlite` (table `tweets`).

Setup: copy `.env.example` to `.env` and fill one key, then restart.

| Key | Provider | Price (2026) |
|---|---|---|
| `TWITTERAPI_IO_KEY` | [twitterapi.io](https://twitterapi.io) (third party) | ≈ $0.15 / 1,000 tweets |
| `X_BEARER_TOKEN` | official X API v2 recent search ([developer.x.com](https://developer.x.com)) | ≈ $5 / 1,000 tweets |

Reads every `X_POLL_SEC` (300 s) and stops at `X_DAILY_CAP` tweets per UTC day (3,000 ≈ $0.45 on twitterapi.io,
200 ≈ $1 on X). Check the key: `.venv/bin/python -m backend.social`. Searches live in `SOCIAL_QUERIES`
(`backend/config.py`). `.env` is git-ignored.

## Layout

| File | What it does |
|---|---|
| `backend/config.py` | Symbols, indicator inputs (Pine defaults), risk/backtest settings |
| `backend/data.py` | Yahoo → SQLite, regular session only, 4h bars anchored 09:30–13:30 / 13:30–16:00 ET, Omni quotes |
| `backend/ict.py` | Bar-by-bar port of the Pine script (FVG, mitigation, pivots, sweeps, BOS/CHoCH, OB, P/D, OR, score) |
| `backend/stats.py` | Setup simulation (limit at FVG edge, stop at far edge, target in R), walk-forward probability, backtest |
| `backend/portfolio.py` | Strength score, long/short pair, SOXL beta hedge, margin split, safe leverage, liquidation prices |
| `backend/loris.py` | Loris Tools funding rates per venue, median, long/short venue spread, own history |
| `backend/social.py` | X (Twitter) reads (twitterapi.io or X API), sentiment, buzz, daily cost cap |
| `backend/server.py` | FastAPI: recalculates on every closed bar, polls Omni every 5 s, WebSocket push |
| `frontend/` | lightweight-charts 4.2.3 UI (light theme, English), browser notifications + sound |

## Assumptions to know

- Omni publishes no maintenance margin per market; `mmr_pct` defaults to 0.5%.
- Omni's API has no candles, so history and indicator bars come from Yahoo (stock price, regular session);
  the Omni mark only ticks the forming candle and prices the suggested legs.
- Pivot ties: the pivot bar must beat the left side strictly and be ≥ the right side.
- One bar touching both stop and target counts as a loss.
- Funding uses today's Omni rate for the whole backtest (Omni publishes no history).
  Swaps: charged at the 17:00 ET close, ×3 on Friday; longs pay the long rate, shorts receive the short rate
  (`shorts_receive` = 0 makes shorts pay instead). Perps: annualized rate × holding time.
- Omni publishes no swap spread or swap leverage cap: 2 bp and x20 are assumed (`spread_bps`, `swap_max_lev`).
- Sizing: fixed risk (`risk_pct`, default 2% to the stop) with `margin_pct` as a margin cap; the backtest
  also shows fixed-margin sizing for comparison.
