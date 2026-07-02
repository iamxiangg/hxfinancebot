# Funnel Review V2

This is the safer candidate-review path for the stock screener.

`Stock Summary USD` stays as the curated master list. New ticker ideas flow into
`BTD_Candidates`, where they are enriched, reviewed in Telegram, and only then
promoted into `Stock Summary USD`.

## Sheets

- `Stock Summary USD`: curated master list. The Telegram approval action appends
  here only when the ticker is not already present.
- `Signal_Log`: raw signal history from Congress and manual seeds.
- `Manual_Seed_Tickers`: optional human-entered ideas. Active rows become manual
  signals.
- `BTD_Candidates`: review queue with yfinance BTD enrichment and optional AI
  Feroldi draft fields. It includes an `Active?` column so open candidates can
  be filtered separately from approved/rejected/archive history. It now also
  carries a judgment layer: attention family, confirmation by source family,
  risk flags, a suggested decision lane, and a one-line thesis summary.
- `Feroldi_AI_Drafts`: append-only log of generated AI drafts when
  `OPENAI_API_KEY` exists.
- `Bot_State`: stores `telegram_last_update_id` so GitHub Actions polling does
  not reprocess old button clicks.
- `Decision_Log`: append-only approve/reject/archive audit log.

## Workflows

- `Funnel - Review Candidates`
  - Scheduled on weekdays at 20:00 Singapore time.
  - Can also be run manually.
  - Refreshes signals, writes candidates, enriches BTD fields, optionally creates
    AI Feroldi drafts, and sends Telegram review cards.
  - Manual runs can set `resend_telegram_reviews=true` to send review cards
    again for candidates that were already notified.

- `Funnel - Telegram Review Bot`
  - Polls every 5 minutes.
  - Processes Telegram inline buttons.
  - Approve promotes into `Stock Summary USD`; reject/archive closes the
    candidate in `BTD_Candidates`.
  - Sends a plain Telegram confirmation message after each processed action.

## Secrets

Required:

- `GCP_SERVICE_ACCOUNT_FILE`
- `GOOGLE_SHEET_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional:

- `OPENAI_API_KEY`
- repository variable `OPENAI_MODEL`

If `OPENAI_API_KEY` is missing, the AI draft step is skipped.

## BTD Candidate Rules

`BTD_Candidates` only receives tickers that are not already in `Stock Summary USD`.
`Signal_Log` can still contain existing master-list tickers because it records
the funnel output before the master-list exclusion.

BTD scoring is lower-is-better:

`EV(B) / (Revenue TTM(B) * Gross Margin(decimal) * Revenue Growth(% points))`

The candidate sheet stores both the final score and the visible components used
to calculate it.

## Judgment Layer

The scanner stack is treated as:

- attention generators: VPMA / PEAD / Political disclosures / Insider
- economic gate: BTD
- business quality filter: Feroldi first cut
- forward confirmation: Fundamental inflection and future estimate-revision layers
- risk / thesis-breaker layer: mixed signals, thin quality coverage, red flags

Each active candidate gets a suggested `Decision Lane`:

- `RESEARCH_NOW`: BTD passed and the thesis has enough independent confirmation
- `WAITING_CONFIRMATION`: promising, but still needs another confirming layer
- `WATCH`: worth monitoring, but quality or completeness is still weak
- `REJECT`: economics did not pass the current minimum hurdle

Telegram review cards now surface this judgment block before the raw BTD and
Feroldi detail so the human review starts from an investing decision, not just
from raw fields.
