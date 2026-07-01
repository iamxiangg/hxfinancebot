from __future__ import annotations

"""Feroldi first-cut 38-point rubric configuration.

All thresholds, lexicons and weights live here so they can be
version-controlled and amended without rewriting the scoring engines.
"""

# ---------------------------------------------------------------------------
# Rubric version
# ---------------------------------------------------------------------------

RUBRIC_VERSION = "FEROLDI-38-V1"
FEROLDI_FIRST_CUT_MAX_POINTS = 38.0

# Section maximums
FINANCIALS_MAX = 17.0
MANAGEMENT_MAX = 10.0
STOCK_MAX = 11.0

# Gate defaults
DEFAULT_PASS_THRESHOLD = 27.5
DEFAULT_REVIEW_THRESHOLD = 23.0
DEFAULT_MIN_COVERAGE = 0.75

# ---------------------------------------------------------------------------
# M03 — Mission statement analysis lexicons
# ---------------------------------------------------------------------------

ACTION_VERBS = frozenset({
    "build", "enable", "connect", "improve", "protect", "simplify",
    "deliver", "create", "help", "empower", "transform", "provide",
    "develop", "make", "advance", "support",
})

VAGUE_TERMS = frozenset({
    "lead", "leading", "innovate", "innovation", "excellence", "best",
    "world-class", "value", "future", "solutions", "impact", "premier",
    "superior",
})

PURPOSE_OUTCOMES = frozenset({
    "access", "health", "safety", "opportunity", "freedom", "knowledge",
    "connection", "sustainability", "productivity", "security",
    "quality of life", "education", "well-being", "wellbeing",
    "inclusion", "affordability", "prosperity",
})

FINANCE_ONLY_PHRASES = frozenset({
    "maximise shareholder value", "maximize shareholder value",
    "increase market share", "become the global leader",
    "deliver superior returns", "drive profitable growth",
    "maximise profits", "maximize profits",
})

# M03 thresholds
MISSION_MIN_WORDS = 5
MISSION_MAX_WORDS = 30
MISSION_MAX_COMMAS = 2
MISSION_MAX_PARENS = 1

# ---------------------------------------------------------------------------
# M01 — CEO extraction markers
# ---------------------------------------------------------------------------

CEO_TITLE_PATTERNS = (
    "chief executive officer",
    "chief executive officer and",
    "ceo",
    "president and chief executive officer",
    "chairman and chief executive officer",
)

FOUNDER_MARKERS = (
    "founded the company",
    "co-founded the company",
    "founder of the company",
    "co-founder of the company",
    "is the founder",
    "is a co-founder",
    "founding",
    "co-founding",
    "established the company",
    "started the company",
)

CEO_SERVED_SINCE_MARKERS = (
    "has served as",
    "served as our chief executive officer",
    "served as the chief executive officer",
    "serves as chief executive officer",
    "has been chief executive officer since",
    "was appointed chief executive officer",
    "was appointed as chief executive officer",
    "was named chief executive officer",
    "became chief executive officer",
    "became our chief executive officer",
    "has been our chief executive officer",
)

INTERIM_MARKERS = (
    "interim chief executive officer",
    "interim ceo",
    "acting chief executive officer",
    "acting ceo",
)

# ---------------------------------------------------------------------------
# S02 — Shareholder actions thresholds
# ---------------------------------------------------------------------------

BUYBACK_DILUTED_SHARE_DECLINE_MIN = 0.01   # 1%
BUYBACK_NET_REPURCHASES_TO_MC_MIN = 0.01   # 1%

DIVIDEND_GROWTH_MIN = 0.02  # 2%
DEBT_DECLINE_MIN = 0.05     # 5%
DEBT_TO_ASSETS_DEBT_FREE_MAX = 0.01  # 1%

# ---------------------------------------------------------------------------
# S03 — Earnings surprise thresholds
# ---------------------------------------------------------------------------

SURPRISE_LARGE_BEAT_PCT = 0.10   # 10%
SURPRISE_LARGE_BEAT_ABS = 0.02   # $0.02
SURPRISE_SMALL_BEAT = 0.5        # half point for small beat

# ---------------------------------------------------------------------------
# M02 — Insider ownership thresholds
# ---------------------------------------------------------------------------

OWNERSHIP_TIER_3 = {"ceo_pct": 0.05, "ceo_stake": 100_000_000, "group_pct": 0.10}   # 3 points
OWNERSHIP_TIER_2 = {"ceo_pct": 0.01, "ceo_stake": 20_000_000, "group_pct": 0.03}     # 2 points
OWNERSHIP_TIER_1 = {"ceo_pct": 0.001, "ceo_stake": 5_000_000, "group_pct": 0.005}    # 1 point

# ---------------------------------------------------------------------------
# M01 tenure buckets
# ---------------------------------------------------------------------------

TENURE_FOUNDER = 4   # score for founder/co-founder/founding-family
TENURE_10_PLUS = 3
TENURE_5_TO_10 = 2
TENURE_2_TO_5 = 1
TENURE_UNDER_2 = 0

# ---------------------------------------------------------------------------
# Enrichment controls
# ---------------------------------------------------------------------------

DEFAULT_ENRICH_LIMIT = 10
DEFAULT_REFRESH_DAYS = 7
DEFAULT_REQUEST_TIMEOUT = 20
