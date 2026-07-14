# Regulatory Lifecycle And Valuation Monitor

## Purpose

This monitor is a deterministic, discovery-first healthcare research system inside `hxfinancebot`. It tracks clinical, regulatory, commercial, reimbursement, ownership, market, and valuation context without producing automatic buy, sell, or sizing recommendations.

## Non-goals

- No BUY, HOLD, SELL, or portfolio-admission output
- No automatic writes into `BTD_Candidates`
- No automatic writes into `Signal_Log`
- No Feroldi or BTD gate interaction
- No LLM, embedding, sentiment, or generative classification path
- No SQL database dependency

## Module Layout

```text
research/regulatory/
providers/regulatory/
funnel/regulatory_*.py
tactical/regulatory_*.py
```

## Why Google Sheets

Google Sheets remains the primary production persistence layer because the rest of the repository already uses it operationally and because the regulatory monitor needs reviewable, easily editable state without adding database infrastructure.

## Why No SQL

The implementation intentionally avoids PostgreSQL, SQLite, SQLAlchemy, migrations, and database credentials. The operational persistence contract is:

- Google Sheets for production tables
- Local JSON for fallback state, audit continuity, and raw payload references

## Source Hierarchy

### Tier 1

- ClinicalTrials.gov
- SEC EDGAR
- Drugs@FDA / FDA application data
- openFDA structured datasets

### Tier 2

- Configured issuer feeds

### Tier 3

- Reserved for future discovery-only sources

## Entity And Ownership Architecture

The monitor resolves:

```text
Public company
-> product
-> regimen
-> indication
-> trial
-> programme
```

Ownership economics remain separate from scientific identity. The code supports manual ownership edges, economic-rights rows, and attributable valuation percentages.

## Programme Identity

Programme identity is keyed by:

```text
company + product + regimen + indication + jurisdiction
```

Different indications and different regimens are intentionally kept separate.

## Parallel State Machines

The monitor tracks:

- Clinical evidence
- Trial operations
- Regulatory
- CMC
- Commercial
- Reimbursement
- Development status
- Legal and IP

State changes are append-only in the gate ledger and projected into a current-state sheet or JSON view.

## Deterministic Rules

Normalization relies on:

- Structured provider fields
- Exact phrase dictionaries
- Explicit regex extraction
- Stable identifiers
- Transparent scoring maps

Ambiguous events are retained in the run output and digest instead of guessed.

## Confidence Dimensions

Each normalized event stores:

- Source confidence
- Mapping confidence
- Evidence grade
- Economic attribution confidence
- Data completeness

## Market-Response Separation

Observed price response is stored independently from fundamental event direction. A positive clinical event can coexist with a negative stock reaction.

## Share-Count Rules

Economic shares prefer:

1. Explicit common shares outstanding
2. Post-offering issued shares
3. Pre-funded warrants
4. Exercised-but-unsettled shares

Weighted-average EPS shares are not used as the preferred current economic share count.

## Valuation Assumptions

Valuation is manual-input-driven. If the required assumptions are missing, the system returns `MODEL_INCOMPLETE`.

## Google Sheets

The actively used operational tabs are auto-created today:

- `Regulatory_Source_State`
- `Regulatory_Events_Raw`
- `Regulatory_Program_Registry`
- `Regulatory_Current`
- `Regulatory_Digest_Log`

## Local JSON Fallback

When Sheets credentials are missing or `REGULATORY_STATE_BACKEND=local`, the system persists state under `funnel_output/regulatory_state`.

## Raw Payload Storage

Raw payloads are written locally under `REGULATORY_AUDIT_DIR/raw_payloads/<source>/`.

## Telegram

The runner always writes a preview. Telegram delivery is optional and chunked. Delivery status is logged as `SENT`, `PARTIAL`, `FAILED`, or `SKIPPED`.

## Bootstrap

Bootstrap runs can look back over recent history without flooding Telegram. Historical discoveries are recorded, not replayed as fresh live alerts.

## Environment Variables

Key variables:

- `REGULATORY_MONITOR_ENABLED`
- `REGULATORY_SOURCES`
- `REGULATORY_STATE_BACKEND`
- `REGULATORY_STATE_DIR`
- `REGULATORY_BOOTSTRAP_LOOKBACK_DAYS`
- `REGULATORY_BOOTSTRAP_SUPPRESS_NOTIFICATIONS`
- `REGULATORY_INCREMENTAL_OVERLAP_DAYS`
- `REGULATORY_SEND_TELEGRAM`
- `REGULATORY_CT_GOV_PAGE_SIZE`
- `REGULATORY_SEC_SIC_ALLOWLIST`
- `REGULATORY_MARKET_SNAPSHOTS_ENABLED`
- `REGULATORY_VALUATION_ENABLED`
- `REGULATORY_AUDIT_DIR`
- `SEC_USER_AGENT`

## Unresolved Handling

Unknown company mappings, ambiguous issuer prose, and incomplete ownership attribution remain unresolved in the in-memory run output, digest preview, and local JSON fallback state. They are not written to a dedicated Google Sheets tab.

## Source Limitations

- ClinicalTrials.gov endpoint behavior can change; the endpoint is configurable with `REGULATORY_CT_GOV_API_URL`
- FDA biologics ingestion is intentionally shipped as an explicit unavailable provider until a stable official machine-readable source is wired in
- Configured issuer feeds are allow-listed only; there is no arbitrary crawler

## Operations

Run locally:

```bash
python -m tactical.regulatory_runner --dry-run --local
```

## Troubleshooting

- If Sheets are unavailable, set `REGULATORY_STATE_BACKEND=local`
- If SEC requests fail, verify `SEC_USER_AGENT`
- If ClinicalTrials.gov returns HTML instead of JSON, override `REGULATORY_CT_GOV_API_URL`
- If valuation stays `MODEL_INCOMPLETE`, populate manual assumption rows first
