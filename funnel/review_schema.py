from __future__ import annotations

from datetime import datetime, timezone


MASTERLIST_SHEET = "Stock Summary USD"
SIGNAL_LOG_SHEET = "Signal_Log"
BTD_CANDIDATES_SHEET = "BTD_Candidates"
FEROLDI_AI_DRAFTS_SHEET = "Feroldi_AI_Drafts"
BOT_STATE_SHEET = "Bot_State"
DECISION_LOG_SHEET = "Decision_Log"
MANUAL_SEED_SHEET = "Manual_Seed_Tickers"
CONGRESS_LEDGER_SHEET = "Congress_Ledger"
INSIDER_LEDGER_SHEET = "Insider_Ledger"


CANDIDATE_ACTIVE_STATUSES = {
    "NEW",
    "ENRICHED",
    "BTD_PASSED",
    "BTD_FAILED",
    "BTD_UNAVAILABLE",
    "BTD_NOT_APPLICABLE",
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
    "Positive Sources",
    "Risk Sources",
    "Corroboration Level",
    "Conflict Status",
    "Supporting Classifications",
    "Supporting Scores",
    "Supporting Reasons",
    "Supporting Signal IDs",
    "Classification",
    "Funnel Score",
    "Signal Count",
    "Discovery Reason",
    "Congress Unique Members",
    "Congress Recent Cluster Members",
    "Congress Active Purchases",
    "Congress Member Names",
    "Insider Total Score",
    "Insider Conviction",
    "Insider Economic Commitment",
    "Insider Market Context",
    "Insider Unique Insiders",
    "Insider Roles",
    "Insider Aggregate Purchase",
    "Insider Cluster Span Days",
    "Insider Weighted Purchase Price",
    "Insider Entry State",
    "First Seen",
    "Last Seen",
    "BTD Score",
    "BTD Ratio",
    "BTD Summary",
    "BTD Applicability",
    "BTD Gate",
    "BTD Gate Reason",
    "Telegram Eligible",
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
    "Active?",
    "EV (B)",
    "Revenue TTM (B)",
    "Gross Margin %",
    "Revenue Growth %",
    "BTD Formula",
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


CONGRESS_LEDGER_HEADERS = [
    "Trade Key",
    "Fingerprint",
    "Ticker",
    "Transaction Date",
    "Filing Date",
    "Last Seen At",
    "Last Seen Payload Hash",
]


INSIDER_LEDGER_HEADERS = [
    "Record Key",
    "Transaction Key",
    "Transaction Group Key",
    "Source Fingerprint",
    "Accession",
    "Issuer CIK",
    "Ticker",
    "Owner CIK",
    "Owner Name",
    "Owner Role",
    "Officer Title",
    "Owner Is Operating",
    "Owner Is Director",
    "Owner Is Officer",
    "Owner Is Ten Percent Owner",
    "Transaction Date",
    "Filing Date",
    "Security Title",
    "Shares",
    "Price Per Share",
    "Transaction Value",
    "Direct Or Indirect",
    "Plan 10b5-1",
    "Shares Owned After",
    "Decision",
    "Reason",
    "Confidence",
    "Qualification Decision",
    "Qualification Reason",
    "Superseded By",
    "Observed At",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
