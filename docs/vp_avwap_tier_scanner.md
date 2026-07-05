# VP/AVWAP Technical Tier Scanner

## Purpose

The HX Earnings-Anchored VP/AVWAP Entry Tier Scanner is an end-of-day decision-support tool for tickers already monitored in `Stock Summary USD`.

- `Stock Summary USD` remains the fundamental eligibility gate.
- This scanner evaluates only current technical-entry attractiveness.
- The scanner does not place trades.
- Route invalidation is not automatically a portfolio stop-loss.
- Technical signals do not guarantee positive investment returns.

## Architecture

- Pure calculations live under `scanners/vp_avwap/`.
- Google Sheets output lives under `funnel/vp_avwap_sheet_writer.py`.
- Console, local artefacts, and optional Telegram delivery live under `tactical/vp_avwap_runner.py`.
- Yahoo Finance requests are routed through `providers.yahoo_throttle.yahoo_download` and `providers.yahoo_throttle.yahoo_call`.
- The ticker universe is loaded from `funnel.sheet_reader.get_stock_summary_ticker_records()`.

## Universe And Sheet Safety

- The watchlist universe comes from the `Stock Summary USD` worksheet.
- `Stock Summary USD` is treated as read-only.
- The scanner writes only `VP_AVWAP_Tiers` and `VP_AVWAP_Entry_Map`.

## Market Data

- Source: Yahoo Finance via `yfinance`
- Daily confirmation basis: latest completed daily candle only
- Trading session: regular hours only with `prepost=False`
- Price basis: unadjusted prices with `auto_adjust=False`

One consistent basis is used across daily OHLC, lower-timeframe OHLC, AVWAP, POC, VAH, VAL, entry zones, invalidation levels, and breakout levels.

## Earnings Anchoring

The scanner uses the latest confirmed past earnings event.

- Before-market earnings anchor to the same regular session.
- After-market earnings anchor to the next regular session.
- During-market earnings anchor to the next regular session for a conservative no-look-ahead daily implementation.
- Unknown timing anchors to the first trading session on or after the earnings date and is marked lower-confidence.

Stored fields:

- `earnings_timestamp`
- `release_timing`
- `reaction_session`
- `reaction_session_confidence`
- `previous_earnings_timestamp`
- `previous_reaction_session`

If no usable confirmed earnings anchor exists, the ticker is marked `DATA_UNAVAILABLE` and forced to Technical Tier 4.

## Data Quality

- `HIGH`: full 30-minute coverage from the current earnings anchor
- `MEDIUM`: full 60-minute coverage from the current earnings anchor
- `LOW`: daily fallback
- `UNAVAILABLE`: insufficient usable data

Hard rule:

- `LOW` data quality cannot rank better than Technical Tier 2.
- `UNAVAILABLE` forces Technical Tier 4.

## Volume Profile Method

The scanner uses an independent deterministic implementation.

- The profile runs from the current earnings reaction session through the latest completed session.
- The full price range is divided into `VP_AVWAP_ROWS` equal-width rows.
- Each bar allocates volume proportionally across every overlapped row.
- Zero-range bars allocate all volume to the row containing HLC3.
- Total allocated volume is conserved within floating-point tolerance.

POC tie-break order:

1. midpoint nearest current AVWAP
2. lower-priced row

Value area expansion is adjacent-only and adds both sides when the next higher and lower rows have equal volume.

## AVWAP Method

- Typical price is HLC3: `(High + Low + Close) / 3`
- AVWAP is cumulative `HLC3 * Volume / cumulative volume`
- End-of-session AVWAP snapshots are derived from the cumulative series
- Five-session slope is `(current AVWAP / AVWAP five completed sessions earlier - 1) * 100`

The scanner also calculates `Previous Anchor VWAP Close` as the final anchored VWAP from the immediately preceding completed earnings-to-earnings period.

## Profile State

Latest completed daily close classification:

- `ABOVE_VAH`
- `UPPER_VALUE_AREA`
- `LOWER_VALUE_AREA`
- `BELOW_VAL`

Derived percentages:

- close vs AVWAP
- close vs POC
- close vs VAH
- close vs VAL

## Entry Routes

The scanner evaluates four independent entry routes:

1. `VAH_DEFENDED_PULLBACK`
2. `POC_AVWAP_RECOVERY`
3. `BREAKOUT_RETEST`
4. `VAL_RECLAIM`

Canonical route statuses:

- `CONFIRMED`
- `TESTING`
- `APPROACHING`
- `WAITING`
- `EXTENDED`
- `FAILED`
- `INVALID`
- `DATA_UNAVAILABLE`

Each route returns:

- zone low/high
- advance alert
- entry trigger price and condition
- route invalidation
- next support
- distance to zone
- risk percentage
- route score
- human-readable reason

## Scoring

Each eligible route is scored out of 100:

- Post-earnings structure: 25
- Level confluence: 25
- Entry readiness: 20
- Price attractiveness: 15
- Route-risk quality: 15

Ticker technical score is the highest eligible route score.

Preferred route tie-break order:

1. `VAH_DEFENDED_PULLBACK`
2. `POC_AVWAP_RECOVERY`
3. `BREAKOUT_RETEST`
4. `VAL_RECLAIM`

## Technical Tiers

Raw score bands:

- Tier 1: `75-100`
- Tier 2: `55-74.99`
- Tier 3: `35-54.99`
- Tier 4: `<35`

Hard overrides include missing anchors, missing market data, below-VAL closes without reclaim, preferred-route failure, low-quality caps, falling-AVWAP caps, and extension caps unless breakout-retest is confirmed.

## Sorting

Results are sorted by:

1. final technical tier ascending
2. technical score descending
3. route-status priority
4. distance to selected buy zone ascending
5. ticker alphabetically

## Google Sheets Output

- Summary worksheet: `VP_AVWAP_Tiers`
- Route-detail worksheet: `VP_AVWAP_Entry_Map`

Before replacing the summary, the writer reads the prior ticker-to-tier mapping, preserves `Previous Technical Tier`, and calculates `Tier Change`.

## Local Artefacts

The runner always writes:

- `funnel_output/vp_avwap/latest_summary.json`
- `funnel_output/vp_avwap/latest_summary.csv`
- `funnel_output/vp_avwap/latest_entry_map.json`
- `funnel_output/vp_avwap/latest_entry_map.csv`
- `funnel_output/vp_avwap/latest_run_metadata.json`

JSON artefacts normalize unavailable values to `null` and never emit `NaN` or `Infinity`.

## Telegram

Telegram delivery is optional and controlled by:

- `VP_AVWAP_SEND_TELEGRAM`
- `VP_AVWAP_TELEGRAM_TEST_MODE`
- `VP_AVWAP_TRADINGVIEW_CHART_ID`

When enabled, the runner sends a Telegram-specific execution queue on every non-dry run. The Telegram layer does not change scanner mathematics, technical scores, tiers, or Sheets output. It is a presentation-only view over the existing VP/AVWAP scan results.

Telegram setup-grade mapping:

- Technical Tier 1 -> `Grade A`
- Technical Tier 2 -> `Grade B`
- Technical Tier 3 -> `Grade C`
- Technical Tier 4 -> `Grade D`

Setup grade means technical quality only. A `Grade A` ticker is not automatically a live buy signal.

Telegram detailed execution buckets:

- `BUY SIGNAL`
- `WAIT FOR DAILY CLOSE`
- `OTHER`

`BUY SIGNAL` means:

- internal Technical Tier 1 / Telegram `Grade A`
- preferred route status is `CONFIRMED`
- completed daily trigger has already occurred
- current price is at or above the entry trigger
- current price is no more than 2% above that trigger

`WAIT FOR DAILY CLOSE` means:

- internal Technical Tier 1 / Telegram `Grade A`
- preferred route status is `TESTING`
- price is currently at the intended buy zone
- no completed daily buy confirmation exists yet

`OTHER` means every remaining ticker, including approaching names, extended names, confirmed but already-stretched names, lower-grade names, failed routes, invalid routes, and unavailable data.

Telegram shows detailed entries only for:

- `BUY SIGNALS`
- `WAIT FOR DAILY CLOSE`

It does not show detailed entries for:

- `APPROACHING`
- `WAITING`
- `EXTENDED`
- `FAILED`
- `INVALID`
- `DATA_UNAVAILABLE`
- `CONFIRMED` setups already more than 2% above the trigger

The Telegram header date is derived from `observed_at_utc`, so the displayed alert date reflects the scan timestamp in UTC rather than the local machine clock.

Telegram wording notes:

- `BUY SIGNAL` is not an instruction to submit an unrestricted market order.
- `Setup fails on daily close below` refers to route invalidation, not automatically to a portfolio stop-loss.
- Position sizing and portfolio risk remain separate decisions outside this scanner.
- Telegram intentionally omits approaching and extended setups to reduce noise.
- Full results remain available in `VP_AVWAP_Tiers`, `VP_AVWAP_Entry_Map`, and the local JSON/CSV artefacts.

Plain-English route labels used in Telegram:

- `VAH_DEFENDED_PULLBACK` -> `Hold Above VAH`
- `POC_AVWAP_RECOVERY` -> `Recover POC/AVWAP`
- `BREAKOUT_RETEST` -> `Breakout Hold`
- `VAL_RECLAIM` -> `Reclaim VAL`

Telegram failure never blocks artefact generation or other ticker processing.
If `Google Ticker` is present, each setup block also includes a TradingView chart URL. Set `VP_AVWAP_TRADINGVIEW_CHART_ID` to reuse a specific TradingView layout, for example `9OmQpc2c`, which yields links like `https://www.tradingview.com/chart/9OmQpc2c/?symbol=NYSE%3AZETA`.

## Environment Variables

- `VP_AVWAP_TEST_TICKERS`
- `VP_AVWAP_MAX_TICKERS`
- `VP_AVWAP_DRY_RUN`
- `VP_AVWAP_WRITE_SHEETS`
- `VP_AVWAP_SEND_TELEGRAM`
- `VP_AVWAP_TELEGRAM_TEST_MODE`
- `VP_AVWAP_TRADINGVIEW_CHART_ID`
- `VP_AVWAP_CALIBRATION`
- `VP_AVWAP_ROWS`
- `VP_AVWAP_VALUE_AREA_PCT`
- `VP_AVWAP_PRIMARY_INTERVAL`
- `VP_AVWAP_SECONDARY_INTERVAL`
- `VP_AVWAP_CONFLUENCE_PCT`
- `VP_AVWAP_ZONE_BUFFER_PCT`
- `VP_AVWAP_APPROACH_PCT`
- `VP_AVWAP_INVALIDATION_BUFFER_PCT`
- `VP_AVWAP_EXTENSION_PCT`
- `VP_AVWAP_AVWAP_SLOPE_LOOKBACK`
- `VP_AVWAP_AVWAP_FLAT_THRESHOLD_PCT`
- `VP_AVWAP_FALLING_OVERRIDE_PCT`
- `VP_AVWAP_BREAKOUT_BUFFER_PCT`
- `VP_AVWAP_BREAKOUT_RETEST_WINDOW`
- `VP_AVWAP_OUTPUT_DIR`

## Run Commands

Dry run:

```bash
VP_AVWAP_TEST_TICKERS="INTC,NVDA,AMD,DDOG" \
VP_AVWAP_DRY_RUN=true \
VP_AVWAP_WRITE_SHEETS=false \
python -m tactical.vp_avwap_runner
```

Production-style run:

```bash
VP_AVWAP_WRITE_SHEETS=true python -m tactical.vp_avwap_runner
```

## Calibration Mode

Set `VP_AVWAP_CALIBRATION=true` to include anchor choice, interval selection, source-range coverage, profile levels, AVWAP levels, value-area percentages, price-adjustment convention, and fallback warnings in the run metadata.

## GitHub Actions

Workflow:

- `.github/workflows/vp_avwap_tiers.yml`

Schedule:

- `23:30 UTC` Monday to Friday

GitHub Actions cron uses UTC.

For Telegram delivery from GitHub Actions, set these repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The workflow now defaults `VP_AVWAP_SEND_TELEGRAM` to `true`. You can override behavior with repository variables:

- `VP_AVWAP_SEND_TELEGRAM`
- `VP_AVWAP_TELEGRAM_TEST_MODE`
- `VP_AVWAP_TRADINGVIEW_CHART_ID`

## Limitations

- DGT is protected-source.
- This code does not reproduce or use DGT source code.
- Exact DGT numerical matching is not guaranteed.
- Yahoo-derived levels may differ from TradingView or DGT.
- Lower-timeframe availability can change POC, VAH, and VAL.
- Daily fallback has reduced data quality.
- Technical Tier is separate from Feroldi and other fundamental scores.
