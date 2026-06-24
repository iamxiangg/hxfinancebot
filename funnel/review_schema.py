from __future__ import annotations

from datetime import datetime, timezone


MASTERLIST_SHEET = "Stock Summary USD"
SIGNAL_LOG_SHEET = "Signal_Log"
BTD_CANDIDATES_SHEET = "BTD_Candidates"
FEROLDI_AI_DRAFTS_SHEET = "Feroldi_AI_Drafts"
BOT_STATE_SHEET = "Bot_State"
DECISION_LOG_SHEET = "Decision_Log"
MANUAL_SEED_SHEET = "Manual_Seed_Tickers"


CANDIDATE_ACTIVE_STATUSES = {
    "NEW",
    "ENRICHED",
    "NOTIFIED",
    "REVIEW",
}

CANDIDATE_FINAL_STATUSES = {
    "APPROVED_ADDED",
    "APPROVED_ALREADY_EXISTS",
    "REJECTED",
    "ARCHIVED",
}


SIGNAL_LOG_HEADERS = [
    "Run ID",
    "Signal ID",
    "Ticker",
    "Source",
    "Classification",
    "Signal Score",
    "Observed At",
    "Valid Until",
    "Reason",
    "Details JSON",
    "Created At",
]


BTD_CANDIDATE_HEADERS = [
    "Candidate ID",
    "Ticker",
    "Company Name",
    "Google Ticker",
    "Status",
    "Review Priority",
    "Source",
    "Classification",
    "Funnel Score",
    "Signal Count",
    "Discovery Reason",
    "First Seen",
    "Last Seen",
    "BTD Score",
    "BTD Ratio",
    "BTD Summary",
    "Next Earnings Date",
    "Enterprise Value",
    "Total Revenue",
    "EBITDA Margin",
    "Revenue Growth",
    "Gross Margin",
    "Employees",
    "BTD Last Updated",
    "AI Feroldi Score",
    "AI Quality Summary",
    "AI Bull Case",
    "AI Bear Case",
    "AI Red Flags",
    "AI Manual Review Needed",
    "AI Confidence",
    "AI Last Updated",
    "Telegram Message ID",
    "Telegram Last Notified At",
    "Decision",
    "Decision At",
    "Decision By",
    "Last Error",
]


FEROLDI_AI_DRAFT_HEADERS = [
    "Candidate ID",
    "Ticker",
    "AI Feroldi Score",
    "Quality Summary",
    "Bull Case",
    "Bear Case",
    "Red Flags",
    "Manual Review Needed",
    "Confidence",
    "Draft JSON",
    "Created At",
]


BOT_STATE_HEADERS = [
    "Key",
    "Value",
    "Updated At",
]


DECISION_LOG_HEADERS = [
    "Decision ID",
    "Candidate ID",
    "Ticker",
    "Action",
    "Actor",
    "Telegram Update ID",
    "Decision At",
    "Result",
    "Details",
]


MANUAL_SEED_HEADERS = [
    "Ticker",
    "Reason",
    "Score",
    "Status",
    "Added At",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
