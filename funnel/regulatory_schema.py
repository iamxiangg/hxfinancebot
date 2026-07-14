from __future__ import annotations


REGULATORY_SOURCE_STATE_SHEET = "Regulatory_Source_State"
REGULATORY_EVENTS_RAW_SHEET = "Regulatory_Events_Raw"
REGULATORY_ENTITY_MAP_SHEET = "Regulatory_Entity_Map"
REGULATORY_OWNERSHIP_SHEET = "Regulatory_Ownership"
REGULATORY_PROGRAM_REGISTRY_SHEET = "Regulatory_Program_Registry"
REGULATORY_GATE_LEDGER_SHEET = "Regulatory_Gate_Ledger"
REGULATORY_CURRENT_SHEET = "Regulatory_Current"
REGULATORY_ECONOMIC_RIGHTS_SHEET = "Regulatory_Economic_Rights"
REGULATORY_FINANCIAL_SNAPSHOTS_SHEET = "Regulatory_Financial_Snapshots"
REGULATORY_MARKET_SNAPSHOTS_SHEET = "Regulatory_Market_Snapshots"
REGULATORY_VALUATION_ASSUMPTIONS_SHEET = "Regulatory_Valuation_Assumptions"
REGULATORY_VALUATION_HISTORY_SHEET = "Regulatory_Valuation_History"
REGULATORY_UNRESOLVED_SHEET = "Regulatory_Unresolved"
REGULATORY_DIGEST_LOG_SHEET = "Regulatory_Digest_Log"


REGULATORY_SOURCE_STATE_HEADERS = [
    "Source Name",
    "Cursor",
    "Last Success At",
    "Last Event At",
    "Bootstrap Complete",
    "Metadata JSON",
]

REGULATORY_EVENTS_RAW_HEADERS = [
    "Raw Event ID",
    "Source Name",
    "Source Record ID",
    "Source URL",
    "Source Document Type",
    "Source Tier",
    "Published At",
    "Observed At",
    "Event Type",
    "Company Name",
    "Ticker",
    "CIK",
    "Product Name",
    "Indication Name",
    "Regimen Name",
    "Trial NCT ID",
    "Jurisdiction",
    "Exact Text",
    "Payload Hash",
    "Payload Path",
    "Amendment Of",
    "Version",
    "Active",
]

REGULATORY_ENTITY_MAP_HEADERS = [
    "Company ID",
    "Legal Name",
    "Ticker",
    "Exchange",
    "CIK",
    "Country",
    "Company Type",
    "Operating Mode",
    "Source URL",
]

REGULATORY_OWNERSHIP_HEADERS = [
    "Ownership Edge ID",
    "Parent Entity ID",
    "Child Entity ID",
    "Parent Ticker",
    "Child Ticker",
    "Legal Relationship",
    "Ownership Percentage",
    "Voting Percentage",
    "Economic Percentage",
    "Consolidation Status",
    "Territory",
    "Effective Date",
    "End Date",
    "Source URL",
    "Confidence",
    "Active",
]

REGULATORY_PROGRAM_REGISTRY_HEADERS = [
    "Programme Key",
    "Company ID",
    "Economic Owner ID",
    "Product ID",
    "Regimen ID",
    "Indication ID",
    "Trial ID",
    "Jurisdiction",
    "Company Name",
    "Ticker",
    "Product Name",
    "Indication Name",
]

REGULATORY_GATE_LEDGER_HEADERS = [
    "Transition ID",
    "Programme Key",
    "Dimension",
    "Prior State",
    "New State",
    "Event ID",
    "Effective At",
    "Reason",
    "Reconstructed",
    "Source URL",
]

REGULATORY_CURRENT_HEADERS = [
    "Programme Key",
    "Company ID",
    "Product ID",
    "Indication ID",
    "Clinical Evidence",
    "Trial Operations",
    "Regulatory",
    "CMC",
    "Commercial",
    "Reimbursement",
    "Development Status",
    "Legal IP",
    "Last Event ID",
    "Last Updated At",
    "Current Gate",
    "Next Catalyst",
    "Catalyst Date",
    "Date Precision",
]

REGULATORY_ECONOMIC_RIGHTS_HEADERS = [
    "Economic Right ID",
    "Programme Key",
    "Company ID",
    "Partner Company ID",
    "Legal Owner",
    "Development Owner",
    "Regulatory Applicant",
    "Commercial Rights Holder",
    "Territory",
    "Royalty Rate",
    "Milestones",
    "License Obligations",
    "Profit Share",
    "Ownership Percentage",
    "Economic Attribution Percentage",
    "Effective Date",
    "End Date",
]

REGULATORY_FINANCIAL_SNAPSHOTS_HEADERS = [
    "Snapshot ID",
    "Company ID",
    "As Of",
    "Common Shares",
    "Pre-Funded Warrants",
    "Traditional Warrants",
    "Options",
    "Convertible Notes",
    "ATM Capacity",
    "Contingent Shares",
    "Issued Shares Post Offering",
    "Exercised Unsettled Shares",
    "Parent Cash",
    "Subsidiary Cash",
    "Restricted Cash",
    "Consolidated Cash",
    "NCI Cash",
    "Attributable Cash",
    "Total Debt",
    "Source URL",
]

REGULATORY_MARKET_SNAPSHOTS_HEADERS = [
    "Snapshot ID",
    "Ticker",
    "Event Date",
    "Previous Close",
    "Event Close",
    "Next Close",
    "Five Session Close",
    "Twenty Session Close",
    "Current Close",
    "SPY Relative Return",
    "XBI Relative Return",
    "Observed Price Direction",
    "Announcement Timing",
    "Timing Confidence",
    "Trading Volume",
]

REGULATORY_VALUATION_ASSUMPTIONS_HEADERS = [
    "Assumption ID",
    "Programme Key",
    "Company ID",
    "Operating Mode",
    "Active",
    "Success EV",
    "Failure EV",
    "Current EV",
    "Launch Year",
    "Approval Probability",
    "Eligible Population",
    "Net Price",
    "Peak Penetration",
    "Peak Sales",
    "Gross Margin",
    "Patent Life Years",
    "Future Dilution",
    "Launch Costs",
    "Manufacturing Scale Up",
    "Sourced Fields JSON",
    "Updated At",
]

REGULATORY_VALUATION_HISTORY_HEADERS = [
    "Valuation ID",
    "Programme Key",
    "Company ID",
    "Valuation Status",
    "Attributable Value",
    "Success EV",
    "Failure EV",
    "Current EV",
    "Market Implied Probability",
    "Equity Value",
    "Per Share Value",
    "Notes JSON",
    "Updated At",
]

REGULATORY_UNRESOLVED_HEADERS = [
    "Unresolved ID",
    "Raw Event ID",
    "Source Record ID",
    "Reason",
    "Source Name",
    "Source URL",
    "Company Name",
    "Ticker",
    "Trial NCT ID",
    "Product Name",
    "Required Action",
    "Conflicting Source",
    "Created At",
]

REGULATORY_DIGEST_LOG_HEADERS = [
    "Digest Date",
    "Event ID",
    "Ticker",
    "Company Name",
    "Product Name",
    "Indication Name",
    "Event Summary",
    "Gate Change",
    "Outcome",
    "Priority",
    "Detailed",
    "Summary Hash",
    "State Hash",
    "Telegram Included",
    "Telegram Delivery Status",
    "Telegram Sent At",
    "Preview Path",
    "Created At",
]
