# Feroldi First-Cut Gate

The Feroldi first-cut score covers three sections:

- Financials: 17 points
- Management and culture: 14 points
- Stock: 11 points
- Full maximum: 42 points

## Why the gate is coverage-aware

Glassdoor and some management inputs may be unavailable even when all financial and stock inputs are complete. A missing four-point Glassdoor score must not be treated as zero.

The candidate table therefore stores both earned points and available points. For example:

`31/38 available (81.6%; 34.3/42 equivalent)`

The equivalent score is used only when the available-point coverage meets the configured minimum.

## Gate bands

The provisional defaults are:

- PASS: at least 30 equivalent points out of 42
- REVIEW: at least 25 but fewer than 30 equivalent points
- FAIL: fewer than 25 equivalent points
- LOW_COVERAGE: fewer than 75% of the 42 points have usable data
- PENDING: the first-cut scorer has not populated score or coverage fields

These thresholds are configuration, not permanent investment rules. They should be calibrated after the existing watchlist has been backfilled.

## Modes

`FEROLDI_GATE_MODE` supports:

- `off`: record the gate as disabled
- `observe`: calculate and record the gate, but preserve the BTD Telegram-eligibility decision
- `enforce`: allow the Feroldi result to control Telegram eligibility

Observe mode is the default. This prevents an uncalibrated score or incomplete data source from silently suppressing candidates.

In enforce mode:

- PASS is eligible for Telegram review
- REVIEW is eligible when `FEROLDI_GATE_ALLOW_REVIEW=true`
- FAIL, LOW_COVERAGE and PENDING are not eligible

## Candidate fields

The Feroldi first-cut scorer should populate:

- `Feroldi Financial Score`
- `Feroldi Financial Available`
- `Feroldi Management Score`
- `Feroldi Management Available`
- `Feroldi Stock Score`
- `Feroldi Stock Available`
- `Feroldi First Cut Score`
- `Feroldi Available Points`
- `Feroldi Missing Inputs`
- `Feroldi Last Updated`

The gate then derives:

- `Feroldi Max Points`
- `Feroldi Equivalent Score`
- `Feroldi Coverage`
- `Feroldi Score Display`
- `Feroldi Gate Mode`
- `Feroldi Gate`
- `Feroldi Gate Reason`

## Rollout sequence

1. Deploy in observe mode.
2. Backfill the 42-point first-cut score for the existing watchlist.
3. Review score distribution, missing-data frequency and false negatives.
4. Adjust the pass/review thresholds if warranted.
5. Change to enforce mode only after calibration.
