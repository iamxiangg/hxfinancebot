## Political Digest Architecture

The Congress scanner remains the single political-disclosure pipeline.

Runtime flow:

1. `scanners/congress/engine.py` downloads the Kadoa payload once and normalises records.
2. `funnel/congress_adapter.py` preserves the existing lightweight `Congress_Ledger` flow for duplicate suppression and `Signal` generation.
3. `funnel/political_archive.py` upserts the durable raw archive, ticker summary rows, bootstrap marker, and digest log with a Google Sheets backend or local JSON fallback under `CONGRESS_STATE_DIR`.
4. `scanners/congress/ticker_history.py` rebuilds deterministic 45/90/365 day ticker histories from the current normalised payload.
5. `scanners/congress/flag_ranker.py` classifies release types, detects probable backfills, compares current summaries with stored state, and ranks digest flags.
6. `tactical/congress_digest.py` renders one logical daily digest and chunks it for Telegram delivery.
7. `tactical/congress_runner.py` sends either the new digest or the legacy formatter, never both unless you explicitly flip the feature flags.

## New Sheets

- `Political_Trades_Raw`: durable current-snapshot archive of every normalised Kadoa transaction, including unresolved tickers and excluded assets.
- `Political_Ticker_Summary`: one current analytical state row per ticker.
- `Political_Digest_Log`: digest decisions, summary hashes, release types, and Telegram inclusion state.

`Congress_Ledger` is unchanged and still acts as lightweight processing memory.

## Bootstrap And Backfill

- Bootstrap marker key: `political_archive_bootstrapped_payload_sha`
- First archive bootstrap writes raw rows and ticker summaries without sending a historical-alert storm.
- Release types use deterministic rules:
  - `LIVE_DISCLOSURE`
  - `LATE_DISCLOSURE`
  - `HISTORICAL_BACKFILL`
  - `MATERIAL_AMENDMENT`
  - `DATA_CORRECTION`
- Probable backfill is triggered when any threshold trips:
  - `POLITICAL_BACKFILL_TRADE_THRESHOLD`
  - `POLITICAL_BACKFILL_FILING_THRESHOLD`
  - `POLITICAL_BACKFILL_TICKER_THRESHOLD`
- During probable backfill, ordinary detailed dossiers are suppressed and the digest focuses on exceptional current disclosures, amendments, or material state changes.

## Classification Model

Primary classifications:

- `INSUFFICIENT_EVIDENCE`
- `SINGLE_FILER_BULLISH_BET`
- `REPEAT_FILER_ACCUMULATION`
- `BROAD_ACCUMULATION`
- `MIXED_HIGH_ACTIVITY`
- `DISTRIBUTION`

Structure classifications:

- `COMMON_STOCK_LED`
- `OPTIONS_LED`
- `MIXED_INSTRUMENT`
- `UNKNOWN_STRUCTURE`

Design notes:

- Purchases and sales are analysed together, but bullish and distribution evidence remain separate.
- Stock, calls, puts, and sales stay in separate amount buckets.
- Lower bounds drive hard materiality checks.
- Midpoints are used only as labelled estimates.
- Households are keyed by `filer_id`.

## Environment Variables

Digest switches:

- `POLITICAL_DIGEST_ENABLED=true`
- `POLITICAL_DIGEST_LEGACY_OUTPUT=false`
- `POLITICAL_DIGEST_SEND_TELEGRAM=true`
- `POLITICAL_DIGEST_SEND_EMPTY=false`
- `POLITICAL_DIGEST_MAX_DETAILED_FLAGS=3`
- `POLITICAL_DIGEST_HARD_MAX_DETAILED_FLAGS=5`

Archive and thresholds:

- `POLITICAL_ARCHIVE_BACKEND=auto`
- `POLITICAL_FLAG_PURCHASE_LOW=100000`
- `POLITICAL_FLAG_CALL_LOW=100000`
- `POLITICAL_FLAG_SALE_LOW=100000`
- `POLITICAL_BROAD_MIN_BUYERS=2`
- `POLITICAL_CONCENTRATION_THRESHOLD=0.70`
- `POLITICAL_BACKFILL_TRADE_THRESHOLD=200`
- `POLITICAL_BACKFILL_FILING_THRESHOLD=25`
- `POLITICAL_BACKFILL_TICKER_THRESHOLD=50`

## Local Fallback

When Sheets are unavailable or `POLITICAL_ARCHIVE_BACKEND=local`, the archive persists under `CONGRESS_STATE_DIR`:

- `political_trades_raw.json`
- `political_ticker_summary.json`
- `political_digest_log.json`
- `bot_state.json`

## Dry Run And Digest Inspection

To render without Telegram delivery:

```bash
POLITICAL_DIGEST_SEND_TELEGRAM=false python -m tactical.congress_runner
```

Preview output lands in:

- `CONGRESS_AUDIT_DIR/political_digest_preview.txt` when `CONGRESS_AUDIT_DIR` is set
- otherwise `funnel_output/political_digest_preview.txt`

## Operational Notes

- The digest does not gate review-funnel admission.
- Existing `Signal` objects still flow into the broader funnel unchanged.
- The Telegram digest uses summary hashes plus `Political_Digest_Log` to suppress unchanged repeat dossiers.
