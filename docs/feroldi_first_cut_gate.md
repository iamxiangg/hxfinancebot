# Feroldi First-Cut Gate — 38-Point Rubric

## Overview

The Feroldi first-cut gate evaluates candidate stocks against a deterministic 38-point rubric across three sections:

- **Financials: 17 points** (F01–F05)
- **Management & Culture: 10 points** (M01–M03)
- **Stock: 11 points** (S01–S03)

Rubric version: `FEROLDI-38-V1`

## Rubric

### Section A — Financials (17 points)

| ID | Criterion | Max | Scoring |
|---|---|---|---|
| F01 | Cash to long-term-debt resilience | 5 | Ratio ≤ 0 → 0, 0<ratio<1 → 1, 1≤ratio<2 → 3, ratio≥2 → 5 |
| F02 | Gross margin | 3 | <50% → 0, 50-65% → 1, 65-80% → 2, ≥80% → 3 |
| F03 | Positive and growing ROE | 3 | Current≤0 → 0, decline → 1, 0-15% growth → 2, ≥15% → 3, turnaround → 2 |
| F04 | Positive and growing FCF | 3 | Same as F03 scoring logic |
| F05 | Positive and growing diluted EPS | 3 | Same as F03 scoring logic |

### Section B — Management & Culture (10 points)

| ID | Criterion | Max | Scoring |
|---|---|---|---|
| M01 | Soul in the game | 4 | Founder → 4, ≥10yr → 3, 5-10yr → 2, 2-5yr → 1, <2yr → 0 |
| M02 | Insider ownership alignment | 3 | CEO ≥5% or stake ≥$100M or group ≥10% → 3, CEO ≥1% or stake ≥$20M or group ≥3% → 2, CEO ≥0.1% or stake ≥$5M or group ≥0.5% → 1 |
| M03 | Mission statement quality | 3 | Simple (1) + Clear (1) + Inspirational (1) |

### Section C — Stock (11 points)

| ID | Criterion | Max | Scoring |
|---|---|---|---|
| S01 | 5-year performance vs SPY | 4 | Excess ≤0 → 0, 0-25pp → 1, 25-50pp → 2, 50-100pp → 3, ≥100pp → 4 |
| S02 | Shareholder-friendly actions | 3 | Buyback (1) + Dividend growth (1) + Debt reduction (1) |
| S03 | Earnings-expectation record | 4 | 1 point per quarter (large beat), 0.5 per small beat, max 4 total |

## Gate Configuration

| Parameter | Default | Env Variable |
|---|---|---|
| Maximum points | 38 | — |
| Pass threshold | 27.5 | `FEROLDI_GATE_PASS_THRESHOLD` |
| Review threshold | 23.0 | `FEROLDI_GATE_REVIEW_THRESHOLD` |
| Minimum coverage | 75% | `FEROLDI_GATE_MIN_COVERAGE` |
| Gate mode | `observe` | `FEROLDI_GATE_MODE` |

## Data Sources

All data is free. No paid API required.

| Source | Used For |
|---|---|
| yfinance | Financial statements, price history, market data |
| SEC EDGAR | CEO evidence (M01), mission statements (M03) |
| SEC XBRL | Financial facts (optional, via existing SEC provider) |

## Module Architecture

```
funnel/feroldi_config.py      — Version, thresholds, lexicons
funnel/feroldi_models.py      — Typed dataclasses for all 11 questions
funnel/feroldi_financials.py  — F01–F05 scoring
funnel/feroldi_management.py  — M01–M03 scoring
funnel/feroldi_mission.py     — M03 deterministic mission analysis (no LLM)
funnel/feroldi_stock.py       — S01–S03 scoring
funnel/feroldi_sec.py         — SEC filing extraction
funnel/feroldi_enrichment.py  — yfinance data collection
funnel/feroldi_scoring.py     — Orchestrator
funnel/feroldi_gate.py        — Coverage-aware gate (updated to 38)
funnel/feroldi_telegram.py    — Telegram rendering (updated to /38)
funnel/review_schema.py       — Sheet headers: Feroldi_First_Cut_Detail
```

## Key Design Principles

1. **No LLM scoring** — All 11 questions use deterministic formulas, not LLMs
2. **No paid API** — yfinance + SEC EDGAR are free
3. **Missing data reduces available points**, never auto-scores zero
4. **M01/M03 use SEC filing evidence** — CEO tenure and mission extracted from official filings
5. **M03 mission analysis is deterministic** — Word counting, lexicon matching, no semantic inference
6. **S02 subtest availability reflects data presence** — Each subtest independently available only when data exists

## Known Limitations

- **Prior period financials (F03/F04/F05)** require quarterly financial statement collection for full growth scoring. Current implementation uses current-only data, limiting growth questions to available=1.
- **S03 earnings surprise** requires quarterly EPS estimates which are not in basic yfinance info. Quarterly earnings data collection is a future enhancement.
- **M02 CEO ownership** requires proxy statement (DEF 14A) extraction, which needs the full filing text. yfinance info alone provides only basic share count, not CEO beneficial ownership.
- **SEC filing caching** is not yet fully implemented. Repeated enrichment calls may re-download filings.
- **Non-US issuers** with no SEC filings will have limited management scoring (M01, M03 unavailable).

## Tests

- `tests/test_feroldi_financials.py` — 37 tests for F01–F05
- `tests/test_feroldi_management.py` — 30 tests for M01–M03 and mission analysis
- `tests/test_feroldi_stock.py` — 29 tests for S01–S03
- `tests/test_feroldi_gate.py` — 9 tests (updated for 38-point)
- `tests/test_feroldi_telegram.py` — 3 tests (updated for /38)

Total: 108 Feroldi-specific tests, all passing. All 458 project tests pass.

## Migration from 42-Point System

- Glassdoor removed completely
- Max points: 42 → 38
- Pass threshold: 30.0/42 → 27.5/38
- Review threshold: 25.0/42 → 23.0/38
- Telegram displays `/38` not `/42`
- Management max: 10 (was part of old 14-point Management)
- All `42` references in active rubric code removed
- Legacy 42-point scores are never normalized into the new rubric
