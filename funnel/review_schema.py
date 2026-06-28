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
REVIEW_REQUESTS_SHEET = "Review_Requests"


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


# --- Review request explicit states (Workstream A3) ---

REVIEW_STATE_PENDING_SEND = "PENDING_SEND"
REVIEW_STATE_SENT = "SENT"
REVIEW_STATE_APPROVED_PENDING_PROMOTION = "APPROVED_PENDING_PROMOTION"
REVIEW_STATE_REJECTED = "REJECTED"
REVIEW_STATE_ARCHIVED = "ARCHIVED"
REVIEW_STATE_EXPIRED = "EXPIRED"
REVIEW_STATE_PROMOTED = "PROMOTED"
REVIEW_STATE_ALREADY_EXISTS = "ALREADY_EXISTS"
REVIEW_STATE_STALE_REVIEW = "STALE_REVIEW"
REVIEW_STATE_FAILED_RETRYABLE = "FAILED_RETRYABLE"

REVIEW_TERMINAL_STATES = frozenset({
    REVIEW_STATE_REJECTED,
    REVIEW_STATE_ARCHIVED,
    REVIEW_STATE_EXPIRED,
    REVIEW_STATE_PROMOTED,
    REVIEW_STATE_ALREADY_EXISTS,
    REVIEW_STATE_STALE_REVIEW,
})

REVIEW_PERMITTED_TRANSITIONS: dict[str, frozenset[str]] = {
    REVIEW_STATE_PENDING_SEND: frozenset({REVIEW_STATE_SENT, REVIEW_STATE_FAILED_RETRYABLE}),
    REVIEW_STATE_SENT: frozenset({
        REVIEW_STATE_APPROVED_PENDING_PROMOTION,
        REVIEW_STATE_REJECTED,
        REVIEW_STATE_ARCHIVED,
        REVIEW_STATE_EXPIRED,
    }),
    REVIEW_STATE_APPROVED_PENDING_PROMOTION: frozenset({
        REVIEW_STATE_PROMOTED,
        REVIEW_STATE_ALREADY_EXISTS,
        REVIEW_STATE_STALE_REVIEW,
        REVIEW_STATE_FAILED_RETRYABLE,
    }),
}


def is_valid_review_transition(current: str, target: str) -> bool:
    return target in REVIEW_PERMITTED_TRANSITIONS.get(current, frozenset())


def is_terminal_review_state(state: str) -> bool:
    return state in REVIEW_TERMINAL_STATES


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


REVIEW_REQUESTS_HEADERS = [
    "Review ID",
    "Candidate ID",
    "Ticker",
    "Candidate Snapshot Hash",
    "State",
    "Issued At",
    "Expires At",
    "Telegram Chat ID",
    "Telegram Message ID",
    "Decision",
    "Decision At",
    "Decision By User ID",
    "Decision By Username",
    "Telegram Update ID",
    "Promotion Result",
    "Promotion At",
    "Last Error",
    "Created At",
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
