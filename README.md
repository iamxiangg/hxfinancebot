# hxfinancebot

This repo now has two distinct layers:

1. `funnel/`
   Long-term stock intake, enrichment, approval, and Google Sheets / Telegram review flow.
2. `scanners/` + `tactical/`
   Modular scanner engines and tactical delivery systems that should stay separate from the long-term funnel unless explicitly integrated.

## Current layout

- `funnel/`
  Candidate review queue, BTD enrichment, Congress / Insider / VPMA adapters, Telegram review bot, Sheets helpers.
- `scanners/congress/`
  Shared Congress scanner engine.
- `scanners/insider/`
  SEC Form 4 insider-buying scanner engine.
- `scanners/vpma/`
  VPMA / PEAD scanner engine and Alpha Vantage enrichment.
- `scanners/earnings/`
  Earnings short-volatility scanner engine.
- `tactical/`
  Tactical runners, state, and Telegram delivery for Congress and earnings notifications.
- `.github/workflows/`
  GitHub Actions entry points for each scanner / bot.

## Entry points

Active wrappers and workflow targets:

- `tactical/congress_runner.py`
- `tactical/earnings_runner.py`
- `funnel/review_candidates.py`
- `funnel/review_bot.py`

Compatibility shims kept for manual or legacy invocation:

- `congress_bot.py`
- `scan_earnings.py`

Archived legacy single-file scripts now live under `archive/`:

- `archive/btd_analysis.py`
- `archive/gamma_scanner.py`
- `archive/scanner_vp_ma_pro.py`
- `archive/gh_bot.py`
- `archive/monitor.py`

## Generated files

The repo intentionally ignores local/generated artifacts such as:

- `funnel_output/`
- `pilot_output/`
- workflow receipts / backups
- earnings universe cache

The one state file that is intentionally expected to be committed by GitHub Actions is:

- `earnings_notification_state.json`

## Cleanup stance

This repo currently mixes older one-file scripts with newer modular scanner packages. The safe direction is:

1. keep working workflow entry points stable;
2. move new work into `scanners/`, `tactical/`, or `funnel/`;
3. retire archived one-file scripts only after their workflows are migrated or replaced.
