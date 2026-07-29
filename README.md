# hxfinancebot

Automated multi-source stock scanner and review funnel. Ingests signals from Congress disclosures, insider Form 4 filings, VPMA/PEAD earnings reactions, and fundamental inflection scanners. Enriches candidates with BTD (buy-the-dip) metrics, Feroldi 38-point deterministic scoring with **multi-year growth trajectory analysis**, and pushes review cards to Google Sheets + Telegram.

**New: FMP Integration** — optional Financial Modeling Prep API key unlocks 3-year growth trajectory scoring (accelerating/stable/decelerating) for ROE, FCF, and EPS. FMP free tier (250 req/day, no credit card) covers the full pipeline with 8× headroom.

**New in PR 1:** Canonical Entity Master, Evidence Ledger with provenance tracking, and no-LLM decision guardrails.

---

## Architecture

```
hxfinancebot/
├── models/             # Shared dataclasses (provenance, entity, evidence)
│   └── common.py       #   SourceEvidence, DerivedValue, EntityMapping, EvidenceRecord
├── scanners/           # Scanner engines (signal generation)
│   ├── congress/       #   Political disclosure scanning
│   ├── insider/        #   SEC Form 4 insider-buying scanning
│   ├── vpma/           #   VPMA/PEAD post-earnings drift scanning
│   ├── earnings/       #   Earnings short-volatility scanning
│   ├── fundamental_inflection/
│                       #   Fundamental-growth inflection scanning
│   ├── entity_master/  #   Canonical entity resolution (CIK-based)
│   ├── evidence_ledger/#   Idempotent evidence ledger with provenance
│   └── no_llm_guard.py #   No-LLM decision guardrails
├── funnel/             # Review funnel (candidate intake, enrichment, notification)
│   ├── review_candidates.py  # Main orchestrator
│   ├── review_bot.py         # Telegram review bot
│   ├── congress_adapter.py
│   ├── insider_adapter.py
│   ├── vpma_adapter.py
│   ├── feroldi_ai.py         # AI-assisted draft generation (narrative-only)
│   ├── feroldi_financials.py # F01–F05 deterministic scoring (trajectory analysis)
│   ├── feroldi_fmp.py        # FMP API client (3-year annual data)
│   ├── feroldi_gate.py       # Feroldi quality gate
│   ├── feroldi_models.py     # Dataclasses (trajectory_label, weighted_growth)
│   └── btd_enrichment.py     # BTD metric enrichment
├── tactical/           # Tactical delivery (per-scan Telegram notifications)
│   ├── congress_runner.py
│   └── earnings_runner.py
├── providers/          # Data providers (SEC EDGAR, etc.)
│   └── sec/
├── docs/               # Architecture and module documentation
│   ├── no_llm_decision_architecture.md
│   ├── entity_master.md
│   └── congress_scanner_refactor.md
├── tests/              # 343 unit and integration tests
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
| **VP/AVWAP** | Earnings-anchored volume-profile and AVWAP entry tiers | `scanners/vp_avwap/engine.py` |
| **Earnings** | Short-volatility earnings plays | `scanners/earnings/engine.py` |
| **Fundamental Inflection** | Revenue/earnings growth inflection points | `scanners/fundamental_inflection/engine.py` |
| **Entity Master** | Canonical entity resolution (CIK-based) | `scanners/entity_master/engine.py` |
| **Evidence Ledger** | Idempotent evidence tracking with provenance | `scanners/evidence_ledger/engine.py` |

### No-LLM Decision Guardrails

`NO_LLM_DECISIONS=true` (default) ensures all investment decisions are deterministic. See `docs/no_llm_decision_architecture.md` for the full specification.

- `scanners/no_llm_guard.py` — Runtime invariants, LLM endpoint blocklist, AI field detection
- AI-generated fields (AI Feroldi Score, AI Quality Summary, etc.) are **narrative-only** and never influence gates, scores, or recommendations
- All deterministic workflows run without `OPENAI_API_KEY`

### Entity Master

Canonical entity layer using SEC CIK as primary identity. See `docs/entity_master.md`.

- **Automatic mappings** only for exact CIK, legal name, or historical ticker matches
- **Fuzzy suggestions** generated for review only — never auto-activated
- **Subsidiary extraction** from Exhibit 21 tables (manual review required)
- **Former names and tickers** extracted from SEC submissions history

### Evidence Ledger

Idempotent evidence ledger with full provenance tracking.

- **Stable evidence IDs** — reproducible hash-based identifiers
- **Idempotent upserts** — duplicate records detected by payload hash
- **Amendment/supersession** — amended records marked inactive with pointer to successor
- **Separate states** — ingested, processed, scored, delivered tracked independently

### Feroldi 38-Point First Cut (deterministic)

The Feroldi first cut is a **zero-LLM, 11-question scoring rubric** that produces a score out of 38 across three categories:

| Category | Questions | Max Points |
|---|---|---|
| **Financials (F01–F05)** | Cash-to-debt, gross margin, ROE, FCF, EPS | 17 |
| **Management (M01–M03)** | CEO tenure/alignment, insider ownership, mission clarity | 10 |
| **Stock (S01–S03)** | 5-year performance vs SPY, buybacks/dividends/debt, earnings surprise | 11 |

Every score is derived from raw numeric inputs using explicit formulas — no qualitative inference, no LLM, no paid API required.

#### Multi-Year Growth Trajectory (F03/F04/F05)

When `FMP_API_KEY` is set (free at [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs), 250 req/day, no credit card), the pipeline extracts 3 years of annual financial data to compute **weighted growth rates** and classify the growth trajectory:

| Trajectory | Condition | Meaning |
|---|---|---|
| `accelerating` | Recent growth > prior × 1.2 | Growth compounding; premium valuation justified |
| `stable` | Recent growth within ±3pp of prior | Predictable compounder |
| `decelerating` | Recent growth < prior × 0.8 | Growth fading; monitor closely |
| `moderate` | Neither accelerating/stable/decelerating | Normal variation |
| `recovering` | Prior growth ≤ 0, recent > 0 | Turnaround in progress |
| `declining` | Prior growth > 0, recent ≤ 0 | Downtrend beginning |

Weighted growth uses 60% recent YoY + 40% prior YoY to smooth volatility. Trajectory labels are written to Google Sheets columns for filtering and sorting.

**Rate limit safety:** FMP free tier allows 250 requests/day. At 10 tickers × 3 endpoints = 30 calls per run, the pipeline has **8× headroom**.

### Review funnel

The `funnel/review_candidates.py` orchestrator:
1. Runs each configured scanner adapter
2. Collects signals into a unified candidate list
3. Enriches with BTD metrics, Feroldi 38-point scoring (with optional FMP trajectory analysis), and Telegram notifications
4. Writes results to Google Sheets (`BTD_Candidates`, `Signal_Log`, `Insider_Ledger`, `Feroldi_First_Cut_Detail`)

Source-level failures (e.g. VPMA crash) do not block other sources — the funnel continues with whatever signals were successfully collected.

---

### Regulatory lifecycle monitor

The repo now also includes a separate deterministic regulatory research system under `research/regulatory/`.

- It monitors clinical, regulatory, issuer, market, and valuation context without using an LLM
- It writes to dedicated regulatory Sheets and local JSON state
- It does **not** automatically feed `BTD_Candidates`, `Signal_Log`, Feroldi gates, or portfolio-decision workflows
- It uses Google Sheets as primary production persistence and local JSON as fallback
- It does **not** introduce PostgreSQL, SQLite, SQLAlchemy, or migrations

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
| `FMP_API_KEY` | Optional. Free API key from [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs) — unlocks 3-year growth trajectory scoring for F03/F04/F05. Free tier: 250 req/day. |

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
| `POLITICAL_DIGEST_ENABLED` | `true` | Enable the daily political-trading digest |
| `POLITICAL_DIGEST_LEGACY_OUTPUT` | `false` | Use the legacy Congress formatter instead of the digest |
| `POLITICAL_DIGEST_SEND_TELEGRAM` | `true` | Render the digest but skip Telegram when false |
| `POLITICAL_DIGEST_SEND_EMPTY` | `true` | Send a success notification when the political scan has no digest hits |
| `POLITICAL_DIGEST_ROLLING_LOOKBACK_DAYS` | `45` | Include qualifying rolling activity whose latest filing is within this many calendar days, even when it is not a fresh disclosure trigger |
| `POLITICAL_DIGEST_ROLLING_TRANSACTION_MAX_AGE_DAYS` | `90` | Exclude rolling activity when the underlying transaction is older than this many calendar days |
| `POLITICAL_DIGEST_MAX_ROLLING_ACTIVITY_ITEMS` | `12` | Max compact rolling late-filing tickers rendered per digest |
| `POLITICAL_DIGEST_MAX_DETAILED_FLAGS` | `3` | Default detailed dossier cap |
| `POLITICAL_DIGEST_HARD_MAX_DETAILED_FLAGS` | `5` | Absolute detailed dossier cap |
| `POLITICAL_DIGEST_WATCHLIST_ENABLED` | `true` | Keep detailed political signals on a short-lived reminder watchlist |
| `POLITICAL_DIGEST_STANDARD_RETENTION_TRADING_DAYS` | `5` | Standard reminder retention in trading-day sessions |
| `POLITICAL_DIGEST_EXCEPTIONAL_RETENTION_TRADING_DAYS` | `10` | Exceptional reminder retention in trading-day sessions |
| `POLITICAL_DIGEST_RISK_RETENTION_TRADING_DAYS` | `5` | Risk reminder retention in trading-day sessions |
| `POLITICAL_DIGEST_MAX_WATCHLIST_ITEMS` | `8` | Max compact reminders rendered per digest |
| `POLITICAL_DIGEST_COMPACT_REMINDER_INTERVAL_DAYS` | `1` | Minimum trading sessions between compact reminders |
| `POLITICAL_DIGEST_REPEAT_FULL_ON_ENTRY_CHANGE` | `true` | Re-alert when entry category changes materially |
| `POLITICAL_DIGEST_REPEAT_FULL_ON_CLASSIFICATION_CHANGE` | `true` | Re-alert when political classification changes materially |
| `POLITICAL_DIGEST_REPEAT_FULL_ON_NEW_TRADE` | `true` | Re-alert when a new material trade refreshes a flagged ticker |
| `POLITICAL_DIGEST_REPEAT_FULL_ON_MATERIAL_AMENDMENT` | `true` | Re-alert when a flagged disclosure is materially amended |
| `POLITICAL_DIGEST_REPEAT_FULL_ON_MAJOR_EVIDENCE_CHANGE` | `true` | Re-alert when supported evidence moves beyond the configured deltas |
| `POLITICAL_DIGEST_SEND_EXPIRED_NOTICE` | `false` | Optionally emit explicit expiry notices when watchlist items lapse |
| `POLITICAL_BACKFILL_TRADE_THRESHOLD` | `200` | Probable backfill threshold by new rows |
| `POLITICAL_BACKFILL_FILING_THRESHOLD` | `25` | Probable backfill threshold by filings |
| `POLITICAL_BACKFILL_TICKER_THRESHOLD` | `50` | Probable backfill threshold by tickers |
| `POLITICAL_FLAG_PURCHASE_LOW` | `100000` | Lower-bound purchase flag threshold |
| `POLITICAL_FLAG_CALL_LOW` | `100000` | Lower-bound call-purchase flag threshold |
| `POLITICAL_FLAG_SALE_LOW` | `100000` | Lower-bound sale flag threshold |
| `POLITICAL_BROAD_MIN_BUYERS` | `2` | Independent-household minimum for broad accumulation |
| `POLITICAL_CONCENTRATION_THRESHOLD` | `0.70` | Concentration threshold for single-filer dominance |

**SEC provider:**

| Variable | Default | Description |
|---|---|---|
| `SEC_PROVIDER` | `official` | `official` (SEC EDGAR) or `edgartools` |
| `SEC_MAX_REQUESTS_PER_SECOND` | 5 | Rate limit |
| `SEC_REQUEST_TIMEOUT` | 30 | Request timeout in seconds |
| `SEC_CACHE_TTL_HOURS` | 24 | Disk cache TTL |
| `SEC_CACHE_DIR` | `funnel_output/sec_cache` | Cache directory |

**Feroldi trajectory scoring:**

| Variable | Default | Description |
|---|---|---|
| `FEROLDI_ENRICH_LIMIT` | 10 | Max tickers to score per run |
| `FEROLDI_FORCE_REFRESH` | `false` | Re-score even if data is recent |
| `FEROLDI_REFRESH_DAYS` | 7 | Days before re-scoring a candidate |
| `FEROLDI_GATE_MODE` | `observe` | `observe` (log only) or `enforce` |
| `FEROLDI_GATE_PASS_THRESHOLD` | 27.5 | Score threshold to pass gate |
| `FEROLDI_GATE_REVIEW_THRESHOLD` | 23.0 | Score threshold for manual review |
| `FEROLDI_GATE_MIN_COVERAGE` | 0.75 | Minimum coverage ratio to evaluate gate |
| `FEROLDI_GATE_ALLOW_REVIEW` | `true` | Allow review recommendations |
| `REGULATORY_MONITOR_ENABLED` | `true` | Enable the regulatory lifecycle monitor |
| `REGULATORY_SOURCES` | `clinicaltrials,sec,drugs_at_fda,openfda,configured_ir` | Regulatory sources to run |
| `REGULATORY_STATE_BACKEND` | `auto` | `auto`, `sheets`, or `local` |
| `REGULATORY_STATE_DIR` | `funnel_output/regulatory_state` | Local fallback state directory |
| `REGULATORY_AUDIT_DIR` | `funnel_output/regulatory_audit` | Preview and raw payload archive directory |
| `REGULATORY_BOOTSTRAP_LOOKBACK_DAYS` | `30` | Bootstrap lookback window |
| `REGULATORY_BOOTSTRAP_SUPPRESS_NOTIFICATIONS` | `true` | Suppress ordinary bootstrap alerts |
| `REGULATORY_INCREMENTAL_OVERLAP_DAYS` | `3` | Incremental overlap window |
| `REGULATORY_SEND_TELEGRAM` | `true` | Enable or disable regulatory Telegram sends |
| `REGULATORY_CT_GOV_PAGE_SIZE` | `50` | ClinicalTrials.gov page size |
| `REGULATORY_SEC_SIC_ALLOWLIST` | `2833,2834,2835,2836,3841,3842,3845,8731` | Healthcare SIC allow-list hint |
| `REGULATORY_MARKET_SNAPSHOTS_ENABLED` | `true` | Capture market snapshots |
| `REGULATORY_VALUATION_ENABLED` | `true` | Enable manual-input-driven valuation |

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

# VP/AVWAP technical tier scanner
VP_AVWAP_TEST_TICKERS="INTC,NVDA,AMD,DDOG" \
VP_AVWAP_DRY_RUN=true \
VP_AVWAP_WRITE_SHEETS=false \
python -m tactical.vp_avwap_runner

# Regulatory lifecycle monitor
python -m tactical.regulatory_runner --dry-run --local
```

### VP/AVWAP technical tiers

The VP/AVWAP scanner reads the monitored universe from `Stock Summary USD`, calculates earnings-anchored volume-profile and AVWAP levels, and writes only `VP_AVWAP_Tiers` and `VP_AVWAP_Entry_Map`.

Its Telegram alert is a presentation-only execution queue layered on top of the same internal scan results:

- `BUY SIGNALS` are Grade A / Tier 1 names with a confirmed daily trigger and a current price no more than 2% above the trigger.
- `WAIT FOR DAILY CLOSE` are Grade A / Tier 1 names currently testing the intended buy zone but still missing the required completed daily confirmation.
- All other names remain available in Google Sheets and local artefacts instead of being listed individually in Telegram.

Production-style run:

```bash
VP_AVWAP_WRITE_SHEETS=true python -m tactical.vp_avwap_runner
```

Local artefacts are written under `funnel_output/vp_avwap/`.

Detailed documentation:

- `docs/vp_avwap_tier_scanner.md`

### Run tests

```bash
# All tests (520)
python -m unittest discover tests -v

# Specific test file
python -m unittest tests.test_vpma_engine -v

# Specific test
python -m unittest tests.test_vpma_engine.ReactionCalculationTests.test_abnormal_return_with_duplicate_index -v

# Focused regulatory monitor tests
python -m unittest tests.test_regulatory_ids tests.test_regulatory_normalizer tests.test_regulatory_state_and_valuation tests.test_regulatory_archive tests.test_regulatory_engine_and_digest -v
```

### Run trajectory batch report

```bash
# Report trajectory labels and weighted growth for a list of tickers
FMP_API_KEY=your_key_here \
  python -c "
from funnel.feroldi_scoring import run_feroldi_first_cut
for ticker in ['AAPL', 'GOOGL', 'NVDA', 'AMZN']:
    d = run_feroldi_first_cut(ticker)
    print(f'{ticker}: F03={d.f03.trajectory_label} F04={d.f04.trajectory_label} F05={d.f05.trajectory_label}')
"
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

### Political digest

The Congress scanner now maintains a durable raw archive (`Political_Trades_Raw`), deterministic ticker history summaries (`Political_Ticker_Summary`), and a repeat-suppressed daily political digest (`Political_Digest_Log`). The existing `Congress_Ledger` remains the lightweight transaction-deduplication layer, while the digest adds separate ticker-state suppression so unchanged political dossiers are not resent every day.

Detailed political signals can remain on an active watchlist for 5 or 10 trading sessions, depending on retention type. Material state changes such as `WAIT -> ACTIONABLE`, supported classification changes, or disclosure amendments can generate a fresh update dossier, while unchanged signals fall back to compact watchlist reminders. Trading-session counting currently uses a deterministic weekday fallback, so weekends are skipped but US market holidays are not modeled separately.

---

## Generated / ignored files

The repo intentionally ignores local artifacts:
- `funnel_output/` — scan receipts, caches, state
- `pilot_output/` — pilot run outputs
- Workflow receipts and backups

Committed by GitHub Actions:
- `earnings_notification_state.json` — earnings notification dedup state

## Documentation

- `docs/no_llm_decision_architecture.md` — No-LLM guardrails specification
- `docs/entity_master.md` — Entity Master architecture and API
- `docs/congress_scanner_refactor.md` — Congress scanner refactor notes
- `docs/political_digest.md` — Political archive, backfill, digest, and dry-run operations

---

## Contributing

1. Keep scanner engines (`scanners/`) free of GitHub Actions, Google Sheets, or Telegram dependencies.
2. Scanner adapters (`funnel/*_adapter.py`) bridge engines into the review funnel.
3. Add tests for any new behavior. Existing test patterns use `unittest` with `unittest.mock`.
4. Do not change existing scoring thresholds without a documented reason.
5. **Never** allow LLM-generated values to influence any investment decision (score, gate, admission, displacement, position sizing, Telegram eligibility).

## Regulatory monitor docs

- `docs/regulatory_lifecycle_architecture.md`
- `python -m tactical.regulatory_runner --dry-run --local`
- Regulatory output is isolated from `BTD_Candidates`, `Signal_Log`, Feroldi gating, and portfolio-decision workflows.
