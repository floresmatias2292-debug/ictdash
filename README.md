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

## Oil strategy (WTI / Brent)

- **Direction (CL, BZ):** Binance TradFi perps CLUSDT (WTI) and BZUSDT (Brent), history since 1 Apr 2026,
  with the same FVG-ICT charts and long/short probability as the other groups. Omni CL-PERP / BZ-PERP give
  the live mark.
- **Delta-neutral pair (USOIL-SWAP / UKOIL-SWAP on Omni):** long one benchmark and short the other, with the
  Brent leg sized at the hedge ratio β, so a move in the oil price cancels out. A setup appears when the
  hedged spread `log(WTI) − β·log(Brent)` is more than 2 standard deviations from its 192-hour mean. It is
  sized so a stop-out loses `risk_pct` of capital. The panel also shows the daily funding carry of each
  orientation at today's Omni rates. The spread statistics come from the Binance perps (no free swap history).
- **Validation (walk-forward, out of sample, net of costs):**
  - CL/BZ probability: AUC 0.52 / 0.51 / 0.49 on 5m / 15m / 1h, i.e. no edge.
  - Pair, fading the spread: 59 trades, 41% hit, −0.15% per trade (loses).
  - Pair, following the stretch: 28 trades, +0.12% per trade, but its 95% range (−0.29%…+0.63%) includes
    zero, so neither is a proven edge.

  Rerun with:

  ```bash
  .venv/bin/python -W ignore research/pair_validate.py revert
  .venv/bin/python -W ignore research/pair_validate.py trend
  .venv/bin/python -W ignore research/prob_validate.py oil
  ```

## Run

Permanent (systemd user service, restarts on failure, starts at login):

```bash
cp deploy/ictdash.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now ictdash.service     # http://localhost:8050
systemctl --user status ictdash.service           # state
journalctl --user -u ictdash.service -f           # live logs
systemctl --user restart ictdash.service          # after code changes
```

Manual: `./run.sh`. The first start backfills Binance (from each listing) and Hyperliquid (newest 5000 candles)
into `data/market.sqlite`; later starts fetch only the missing bars. Python deps: pandas numpy fastapi uvicorn httpx
in `.venv` (recreate it if the folder is moved).

## Layout

| File | What it does |
|---|---|
| `backend/config.py` | Symbols, indicator inputs (Pine defaults), risk/backtest settings |
| `backend/data.py` | Yahoo → SQLite, regular session only, 4h bars anchored 09:30–13:30 / 13:30–16:00 ET, Omni quotes |
| `backend/ict.py` | Bar-by-bar port of the Pine script (FVG, mitigation, pivots, sweeps, BOS/CHoCH, OB, P/D, OR, score) |
| `backend/stats.py` | Setup simulation (limit at FVG edge, stop at far edge, target in R), walk-forward probability, backtest |
| `backend/portfolio.py` | Strength score, long/short pair, SOXL beta hedge, margin split, safe leverage, liquidation prices |
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
