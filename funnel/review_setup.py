from __future__ import annotations

from funnel.review_schema import (
    BOT_STATE_HEADERS,
    BOT_STATE_SHEET,
    CONGRESS_LEDGER_HEADERS,
    CONGRESS_LEDGER_SHEET,
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    DECISION_LOG_HEADERS,
    DECISION_LOG_SHEET,
    FEROLDI_AI_DRAFT_HEADERS,
    FEROLDI_AI_DRAFTS_SHEET,
    INSIDER_LEDGER_HEADERS,
    INSIDER_LEDGER_SHEET,
    MANUAL_SEED_HEADERS,
    MANUAL_SEED_SHEET,
    SIGNAL_LOG_HEADERS,
    SIGNAL_LOG_SHEET,
)
from funnel.sheet_table import ensure_sheet


def ensure_review_sheets(service, spreadsheet_id: str) -> None:
    sheets = [
        (SIGNAL_LOG_SHEET, SIGNAL_LOG_HEADERS),
        (BTD_CANDIDATES_SHEET, BTD_CANDIDATE_HEADERS),
        (FEROLDI_AI_DRAFTS_SHEET, FEROLDI_AI_DRAFT_HEADERS),
        (BOT_STATE_SHEET, BOT_STATE_HEADERS),
        (DECISION_LOG_SHEET, DECISION_LOG_HEADERS),
        (MANUAL_SEED_SHEET, MANUAL_SEED_HEADERS),
        (CONGRESS_LEDGER_SHEET, CONGRESS_LEDGER_HEADERS),
        (INSIDER_LEDGER_SHEET, INSIDER_LEDGER_HEADERS),
    ]
    for sheet_name, headers in sheets:
        ensure_sheet(service, spreadsheet_id, sheet_name, headers)
