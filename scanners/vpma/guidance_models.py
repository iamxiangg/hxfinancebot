from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class EvidenceItem:
    field: str
    extracted_value: Any
    accession: str
    document: str
    section: str
    supporting_text: str
    extraction_method: str
    confidence: str


@dataclass
class EarningsFundamentalConfirmation:
    ticker: str
    earnings_date: date
    source_accession: str | None
    economic_classification: str

    reported_revenue: float | None = None
    revenue_growth_yoy: float | None = None
    gross_margin_pct: float | None = None
    gross_margin_change_bps: float | None = None
    operating_margin_pct: float | None = None
    operating_margin_change_bps: float | None = None
    free_cash_flow: float | None = None

    revenue_guidance_action: str = "UNAVAILABLE"
    revenue_guidance_low: float | None = None
    revenue_guidance_high: float | None = None
    revenue_guidance_midpoint: float | None = None
    prior_revenue_guidance_midpoint: float | None = None
    revenue_guidance_change_pct: float | None = None

    margin_guidance_action: str = "UNAVAILABLE"
    margin_guidance_change_bps: float | None = None

    business_kpis: dict[str, float | str] = field(default_factory=dict)
    score: float = 0.0
    confidence: str = "low"
    conflict_flags: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)


BUSINESS_KPI_MAP: dict[str, list[str]] = {
    "SaaS": [
        "RPO", "current RPO", "ARR", "net revenue retention",
        "large-customer count", "subscription revenue",
    ],
    "Payments": [
        "TPV", "active customers", "transaction margin",
        "take rate", "credit losses", "payment volume",
    ],
    "Fintech": [
        "TPV", "active customers", "transaction margin",
        "take rate", "credit losses", "payment volume",
    ],
    "Payments/Fintech": [
        "TPV", "active customers", "transaction margin",
        "take rate", "credit losses", "payment volume",
    ],
    "Marketplace": [
        "GMV", "active buyers", "order frequency",
        "contribution margin", "take rate",
    ],
    "Semiconductors": [
        "segment revenue", "data centre revenue", "data-center revenue",
        "datacenter revenue", "inventory", "gross margin",
        "customer concentration", "AI revenue",
    ],
    "Consumer": [
        "same-store sales", "comparable sales", "active customers",
        "subscription revenue", "e-commerce revenue",
        "digital revenue",
    ],
    "Financials": [
        "net interest margin", "loan growth", "deposits",
        "assets under management", "AUM", "return on equity",
        "book value per share",
    ],
    "Healthcare": [
        "product revenue", "royalty revenue", "pipeline progress",
        "regulatory milestones", "sales growth",
    ],
    "Energy": [
        "production", "realised price", "capital expenditure",
        "free cash flow", "operating cash flow",
    ],
}


def kpi_candidates_for_industry(industry: str) -> list[str]:
    industry_key = str(industry or "").strip()
    for key, candidates in BUSINESS_KPI_MAP.items():
        if key.lower() in industry_key.lower():
            return list(candidates)
    return []


GUIDANCE_TEXT_PATTERNS: dict[str, list[str]] = {
    "RAISED": ["raises", "raising", "raised", "increase", "increases", "increasing", "increased",
               "raises guidance", "raising guidance", "raised guidance", "increase guidance",
               "upward revision", "upwardly revised", "better than expected"],
    "LOWERED": ["lowers", "lowering", "lowered", "decreases", "decreasing", "decreased",
                "lowers guidance", "lowering guidance", "lowered guidance", "decrease guidance",
                "downward revision", "downwardly revised"],
    "MAINTAINED": ["reaffirms", "reaffirming", "reaffirmed", "maintains", "maintaining",
                   "maintained", "unchanged", "no change", "in line with", "consistent with",
                   "reiterates", "reiterated"],
    "WITHDRAWN": ["withdraws", "withdrawing", "withdrawn", "suspends guidance", "suspended guidance",
                  "no longer providing", "not providing guidance"],
}
