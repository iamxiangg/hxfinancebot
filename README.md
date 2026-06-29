# hxfinancebot

Automated multi-source stock scanner and review funnel. Ingests signals from Congress disclosures, insider Form 4 filings, VPMA/PEAD earnings reactions, and fundamental inflection scanners. Enriches candidates with BTD (buy-the-dip) metrics, Feroldi AI scoring, and pushes review cards to Google Sheets + Telegram.

---

## Architecture

```
hxfinancebot/
├── scanners/           # Scanner engines (signal generation)
│   ├── congress/       #   Political disclosure scanning
│   ├── insider/        #   SEC Form 4 insider-buying scanning
│   ├── vpma/           #   VPMA/PEAD post-earnings drift scanning
│   ├── earnings/       #   Earnings short-volatility scanning
│   └── fundamental_inflection/
│                       #   Fundamental-growth inflection scanning
├── funnel/             # Review funnel (candidate intake, enrichment, notification)
│   ├── review_candidates.py  # Main orchestrator
│   ├── review_bot.py         # Telegram review bot
│   ├── congress_adapter.py
│   ├── insider_adapter.py
│   ├── vpma_adapter.py
│   ├── feroldi_ai.py         # AI-assisted draft generation
│   ├── feroldi_gate.py       # Feroldi quality gate
│   └── btd_enrichment.py     # BTD metric enrichment
├── tactical/           # Tactical delivery (per-scan Telegram notifications)
│   ├── congress_runner.py
│   └── earnings_runner.py
├── providers/          # Data providers (SEC EDGAR, etc.)
│   └── sec/
├── tests/              # 261 unit and integration tests
├── config/             # Static configuration overrides
├── archive/            # Legacy single-file scripts (retired)
└── .github/workflows/  # GitHub Actions CI entry points
```

### Scanner engines

Each scanner under `scanners/` is a self-contained engine with no GitHub Actions or Google Sheets dependencies:

| Scanner | What it detects | Key files |
|---|---|---|
| **Congress** | Political disclosure trades (House/Senate) | `scanners/congress/engine.py` |
| **Insider** | SEC Form 4 open-market insider purchases | `scanners/insider/engine.py` |
| **VPMA** | Post-earnings-announcement drift (PEAD) setups | `scanners/vpma/engine.py` |
| **Earnings** | Short-volatility earnings plays | `scanners/earnings/engine.py` |
| **Fundamental Inflection** | Revenue/earnings growth inflection points | `scanners/fundamental_inflection/engine.py` |

### Review funnel

The `funnel/review_candidates.py` orchestrator:
1. Runs each configured scanner adapter
2. Collects signals into a unified candidate list
3. Enriches with BTD metrics, Feroldi AI scoring, and Telegram notifications
4. Writes results to Google Sheets (`BTD_Candidates`, `Signal_Log`, `Insider_Ledger`)

Source-level failures (e.g. VPMA crash) do not block other sources — the funnel continues with whatever signals were successfully collected.

---

## Setup

### Requirements

- Python 3.10+
- Dependencies: `pip install -r requirements.txt`

### Environment variables

Copy these to your GitHub Actions repository variables or a local `.env` file:

**Core credentials:**

| Variable | Required | Notes |
|---|---|---|
| `GCP_SERVICE_ACCOUNT_FILE` | Yes | Path to Google service account JSON for Sheets API |
| `GOOGLE_SHEET_ID` | Yes | Google Sheets spreadsheet ID |
| `OPENAI_API_KEY` | Optional | For Feroldi AI draft generation |
| `TELEGRAM_BOT_TOKEN` | Optional | For Telegram review notifications |
| `TELEGRAM_CHAT_ID` | Optional | Target Telegram chat |

**SEC EDGAR access (required for insider scanner):**

| Variable | Notes |
|---|---|
| `SEC_USER_AGENT` | A descriptive contact string, e.g. `hxfinancebot contact@example.com`. This is **not** an account or API key — it follows SEC EDGAR fair-access rules. Required for production; tests use a fallback. |

**Scanner configuration (all optional, sensible defaults):**

| Variable | Default | Description |
|---|---|---|
| `INSIDER_LOOKBACK_DAYS` | 7 | Business days of SEC daily index to scan |
| `INSIDER_HISTORY_DAYS` | 365 | Days of purchase history for cluster detection |
| `INSIDER_CLUSTER_DAYS` | 21 | Max days between insider purchases in a cluster |
| `VPMA_EVENT_LOOKBACK_DAYS` | 90 | Days to look back for earnings events |
| `VPMA_VALID_DAYS` | 3 | Signal validity period |
| `VPMA_TEST_TICKERS` | — | Comma-separated tickers for testing (skips universe download) |
| `REVIEW_SOURCES` | `congress,vpma,insider,fundamental_inflection,manual` | Which scanners to run |
| `BTD_GATE_THRESHOLD` | 1.0 | BTD ratio threshold |
| `FEROLDI_GATE_MODE` | `observe` | `observe` or `enforce` |
| `SEND_TELEGRAM_REVIEWS` | `true` | Enable/disable Telegram notifications |

**SEC provider:**

| Variable | Default | Description |
|---|---|---|
| `SEC_PROVIDER` | `official` | `official` (SEC EDGAR) or `edgartools` |
| `SEC_MAX_REQUESTS_PER_SECOND` | 5 | Rate limit |
| `SEC_REQUEST_TIMEOUT` | 30 | Request timeout in seconds |
| `SEC_CACHE_TTL_HOURS` | 24 | Disk cache TTL |
| `SEC_CACHE_DIR` | `funnel_output/sec_cache` | Cache directory |

---

## Running

### Run all scanners and update the review funnel

```bash
python funnel/review_candidates.py
```

This is the main entry point. It runs all configured scanner sources, classifies signals, enriches candidates, and updates Google Sheets.

### Run a single scanner for testing

```bash
# VPMA with specific tickers (skips universe download)
VPMA_TEST_TICKERS="AAPL,MSFT,NVDA" python -c "from scanners.vpma.engine import run_vpma_scan; print(run_vpma_scan())"

# Congress scanner
python tactical/congress_runner.py

# Earnings scanner
python tactical/earnings_runner.py
```

### Run tests

```bash
# All tests (261)
python -m unittest discover tests -v

# Specific test file
python -m unittest tests.test_vpma_engine -v

# Specific test
python -m unittest tests.test_vpma_engine.ReactionCalculationTests.test_abnormal_return_with_duplicate_index -v
```

---

## Key behaviors

### Insider scanner date selection

The insider scanner uses **US business days** (Mon-Fri) based on `America/New_York` time. It does not request the current SEC daily index until it is expected to be published (typically after 10 PM ET). Weekends are skipped without consuming lookback slots. A missing index file (holiday, SEC delay) skips that date and continues with the next.

### Per-ticker failure isolation (VPMA)

A single ticker with bad data, a malformed symbol, or a calculation error cannot crash the entire VPMA scan. Failures are categorized (`invalid_symbol`, `missing_market_data`, `calculation_rejected`, `unexpected_errors`) and logged in the scan summary.

### SEC access errors

- **HTTP 404** (index file not found): logged and skipped — common for weekends, holidays, or before the SEC publishes the day's index.
- **HTTP 403** (access denied): retried with exponential backoff. If all attempted business dates return 403, the scan fails loudly with a clear message to check `SEC_USER_AGENT`.
- **HTTP 429/5xx**: retried with bounded exponential backoff (8-second cap).

### Insider Ledger

The insider scanner persists all processed SEC accessions and qualified purchases to Google Sheets (`Insider_Ledger` sheet) or local JSON (`funnel_output/insider_state/`). Processed accessions are deduplicated across runs. Form 4/A amendments supersede prior versions. Purchases are clustered across 21-day windows for scoring.

---

## Generated / ignored files

The repo intentionally ignores local artifacts:
- `funnel_output/` — scan receipts, caches, state
- `pilot_output/` — pilot run outputs
- Workflow receipts and backups

Committed by GitHub Actions:
- `earnings_notification_state.json` — earnings notification dedup state

---

## Contributing

1. Keep scanner engines (`scanners/`) free of GitHub Actions, Google Sheets, or Telegram dependencies.
2. Scanner adapters (`funnel/*_adapter.py`) bridge engines into the review funnel.
3. Add tests for any new behavior. Existing test patterns use `unittest` with `unittest.mock`.
4. Do not change existing scoring thresholds without a documented reason.
