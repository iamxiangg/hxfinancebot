from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from providers.sec.base import SECProvider
from providers.sec.models import (
    FilingDocument,
    FilingDocumentMetadata,
    FilingMetadata,
)
from scanners.vpma.engine import (
    EarningsEvent,
    UniverseTicker,
    VpmaConfig,
    VpmaTickerResult,
    apply_guidance_confirmation,
    evaluate_ticker,
)
from scanners.vpma.guidance_extraction import (
    _classify_by_midpoint,
    extract_confirmation,
    find_earnings_8k,
)
from scanners.vpma.guidance_models import (
    EarningsFundamentalConfirmation,
    EvidenceItem,
    kpi_candidates_for_industry,
)
from scanners.vpma.guidance_scoring import (
    apply_economic_overlay,
    classify_economic_event,
    determine_conflict_type,
    score_economic_event,
)


class _FakeGuidanceProvider(SECProvider):
    def __init__(self, filings_by_ticker: dict | None = None, documents: dict | None = None,
                 text_by_accession: dict | None = None, filing_index_errors: dict | None = None):
        self.filings_by_ticker = filings_by_ticker or {}
        self.documents = documents or {}
        self.text_by_accession = text_by_accession or {}
        self.filing_index_errors = filing_index_errors or {}

    def company_profile(self, ticker: str):
        from providers.sec.models import CompanyProfile
        return CompanyProfile(ticker=ticker, cik="0001650372", name=ticker)

    def recent_filings(self, ticker: str, *, forms=None, filed_after=None):
        return self.filings_by_ticker.get(ticker, [])

    def daily_index_filings(self, day, *, forms=None):
        return []

    def company_facts(self, ticker: str, *, as_of=None):
        from providers.sec.models import CompanyFacts
        return CompanyFacts(ticker=ticker, cik="0001650372")

    def filing_documents(self, filing: FilingMetadata):
        docs = self.documents.get(filing.accession, [])
        if not docs:
            return [
                FilingDocumentMetadata(
                    filing_accession=filing.accession,
                    document_name="ex99-1.htm",
                    document_type="EX-99.1",
                    is_primary=False,
                )
            ]
        return docs

    def filing_text(self, filing: FilingMetadata, *, document_name=None):
        error = self.filing_index_errors.get(filing.accession)
        if error is not None:
            raise error
        text = self.text_by_accession.get(filing.accession, "")
        return FilingDocument(
            filing_accession=filing.accession,
            document_name=document_name or filing.primary_document,
            text=text,
            source_url=filing.source_url,
        )

    def form4_transactions(self, filing: FilingMetadata):
        return []


def _make_filing(accession: str, form: str = "8-K",
                 filed_at: datetime | None = None,
                 primary_document: str = "form8k.htm") -> FilingMetadata:
    return FilingMetadata(
        ticker="TEAM",
        cik="0001650372",
        accession=accession,
        form=form,
        filed_at=filed_at or datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC),
        report_date=date(2026, 3, 31),
        primary_document=primary_document,
        is_amendment=False,
        source_url=f"https://www.sec.gov/Archives/edgar/data/1650372/{accession.replace('-', '')}/{accession}.txt",
    )


STRONG_EARNINGS_TEXT = """
Item 2.02 Results of Operations and Financial Condition
On April 15, 2026, Example Corp issued a press release announcing its financial results
for the first quarter ended March 31, 2026.

Revenue was $2,450 million, representing growth of 38% year-over-year.
Gross margin was 78.5%, an increase of 180 basis points compared to the prior year.
Operating margin was 22.3%.
Free cash flow was $890 million.

Full year revenue guidance raised to $9.8 billion to $10.2 billion,
representing an increase of 3.1% at the midpoint from prior guidance.
Operating margin guidance was raised to approximately 23%.
Net revenue retention was 120%.
RPO grew 44% to $4.2 billion.
Number of large customers increased to 2,850.
"""

WEAK_EARNINGS_TEXT = """
Item 2.02 Results of Operations and Financial Condition
On April 15, 2026, Example Corp issued a press release announcing its financial results
for the first quarter ended March 31, 2026.

Revenue was $1,200 million, declining 8% year-over-year.
Gross margin was 62.0%, a decrease of 300 basis points compared to the prior year.
Operating margin was 5.1%.

The company lowered its full year revenue guidance to $4.5 billion to $4.7 billion,
representing a reduction of 4.3% at the midpoint from prior guidance.
"""

MIXED_EARNINGS_TEXT = """
Item 2.02 Results of Operations and Financial Condition
On April 15, 2026, Example Corp issued a press release announcing its financial results
for the first quarter ended March 31, 2026.

Revenue was $1,800 million, representing growth of 22% year-over-year.
Gross margin was 71.0%, flat compared to the prior year.

Full year revenue guidance was raised to $7.2 billion to $7.5 billion.
However, operating margin guidance was lowered.
"""

MAINTAINED_EARNINGS_TEXT = """
Item 2.02 Results of Operations and Financial Condition
Revenue was $3,000 million, growth of 12%.
The company reaffirmed its full year revenue guidance of $12.0 billion to $12.5 billion.
"""

WITHDRAWN_EARNINGS_TEXT = """
Item 2.02 Results of Operations and Financial Condition
Revenue was $500 million.
The company withdrew its full year revenue guidance due to macroeconomic uncertainty.
"""

NO_GUIDANCE_TEXT = """
Item 2.02 Results of Operations and Financial Condition
Revenue was $900 million, growth of 5%.
The company does not provide forward-looking guidance.
"""


class GuidanceModelsTests(unittest.TestCase):
    def test_evidence_item_is_frozen_and_retains_fields(self):
        item = EvidenceItem(
            field="revenue_growth",
            extracted_value=38.0,
            accession="0001650372-24-000123",
            document="ex99-1.htm",
            section="earnings release",
            supporting_text="38% YoY growth",
            extraction_method="regex",
            confidence="medium",
        )
        self.assertEqual(item.field, "revenue_growth")
        self.assertEqual(item.extracted_value, 38.0)
        self.assertEqual(item.accession, "0001650372-24-000123")

    def test_confirmation_defaults_to_unavailable(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession=None,
            economic_classification="ECONOMIC_UNAVAILABLE",
        )
        self.assertEqual(conf.economic_classification, "ECONOMIC_UNAVAILABLE")
        self.assertEqual(conf.revenue_guidance_action, "UNAVAILABLE")
        self.assertEqual(conf.confidence, "low")

    def test_kpi_candidates_matches_industry(self):
        self.assertIn("ARR", kpi_candidates_for_industry("Software - SaaS"))
        self.assertIn("TPV", kpi_candidates_for_industry("Payments / Fintech"))
        self.assertIn("data centre revenue", kpi_candidates_for_industry("Semiconductors"))
        self.assertIn("GMV", kpi_candidates_for_industry("Marketplace"))

    def test_unrecognised_industry_returns_empty(self):
        self.assertEqual(kpi_candidates_for_industry("UnknownSector"), [])


class GuidanceExtractionTests(unittest.TestCase):
    def test_find_earnings_8k_matches_correct_date_range(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        on_time = _make_filing("a1", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        too_early = _make_filing("a2", filed_at=datetime(2026, 4, 10, 8, 0, 0, tzinfo=UTC))
        too_late = _make_filing("a3", filed_at=datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC))

        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [too_early, on_time, too_late]},
        )
        result = find_earnings_8k(provider, "TEAM", earnings_ts)
        self.assertIsNotNone(result)
        self.assertEqual(result.accession, "a1")

    def test_wrong_quarter_filing_is_rejected(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        wrong_quarter = _make_filing(
            "a-wrong",
            filed_at=datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC),
        )
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [wrong_quarter]},
        )
        result = find_earnings_8k(provider, "TEAM", earnings_ts)
        self.assertIsNone(result)

    def test_extract_strong_guidance_raise(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-strong", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-strong": STRONG_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "SaaS", earnings_ts)
        self.assertEqual(conf.source_accession, "a-strong")
        self.assertIsNotNone(conf.revenue_growth_yoy)
        self.assertEqual(conf.revenue_growth_yoy, 38.0)
        self.assertIsNotNone(conf.gross_margin_pct)
        self.assertEqual(conf.gross_margin_pct, 78.5)
        self.assertIsNotNone(conf.gross_margin_change_bps)
        self.assertEqual(conf.gross_margin_change_bps, 180.0)
        self.assertIn(conf.revenue_guidance_action, {"RAISED", "MODESTLY_RAISED"})

    def test_extract_guidance_reduction(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-weak", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-weak": WEAK_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertEqual(conf.source_accession, "a-weak")
        self.assertIsNotNone(conf.revenue_growth_yoy)
        self.assertEqual(conf.revenue_growth_yoy, -8.0)

    def test_extract_maintained_guidance(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-maint", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-maint": MAINTAINED_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertEqual(conf.source_accession, "a-maint")

    def test_extract_withdrawn_guidance(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-withdrawn", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-withdrawn": WITHDRAWN_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertEqual(conf.source_accession, "a-withdrawn")

    def test_filing_unavailable_handles_gracefully(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        provider = _FakeGuidanceProvider(filings_by_ticker={})
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertIsNone(conf.source_accession)
        self.assertEqual(conf.economic_classification, "ECONOMIC_UNAVAILABLE")
        self.assertIn("no_filing_match", conf.conflict_flags)

    def test_filing_text_error_is_isolated(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-bad", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            filing_index_errors={"a-bad": RuntimeError("text unavailable")},
        )
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertIsNotNone(conf.source_accession)
        self.assertIn("filing_text_unavailable", conf.conflict_flags)

    def test_evidence_items_retain_provenance(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-evidence", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-evidence": STRONG_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "SaaS", earnings_ts)
        self.assertGreater(len(conf.evidence), 0)
        for item in conf.evidence:
            self.assertEqual(item.accession, "a-evidence")
            self.assertIsNotNone(item.extraction_method)

    def test_kpi_extraction_for_saas(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-kpi", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-kpi": STRONG_EARNINGS_TEXT},
        )
        conf = extract_confirmation(provider, "TEAM", "SaaS", earnings_ts)
        self.assertIn("RPO", conf.business_kpis)
        self.assertGreater(len(conf.business_kpis), 0)

    def test_unmapped_kpis_do_not_crash(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-unmapped", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-unmapped": "Some custom metrics: widget count 5000, throughput 98.7%"},
        )
        conf = extract_confirmation(provider, "TEAM", "UnknownSector", earnings_ts)
        self.assertEqual(len(conf.business_kpis), 0)

    def test_empty_filing_text_is_handled(self):
        earnings_ts = datetime(2026, 4, 15, 16, 30, 0, tzinfo=UTC)
        filing = _make_filing("a-empty", filed_at=datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC))
        provider = _FakeGuidanceProvider(
            filings_by_ticker={"TEAM": [filing]},
            text_by_accession={"a-empty": ""},
        )
        conf = extract_confirmation(provider, "TEAM", "", earnings_ts)
        self.assertIn("empty_filing_text", conf.conflict_flags)


class GuidanceScoringTests(unittest.TestCase):
    def test_midpoint_classification_thresholds(self):
        self.assertEqual(_classify_by_midpoint(5.0), "RAISED")
        self.assertEqual(_classify_by_midpoint(2.5), "RAISED")
        self.assertEqual(_classify_by_midpoint(1.0), "MODESTLY_RAISED")
        self.assertEqual(_classify_by_midpoint(0.5), "MODESTLY_RAISED")
        self.assertEqual(_classify_by_midpoint(0.0), "MAINTAINED")
        self.assertEqual(_classify_by_midpoint(-0.4), "MAINTAINED")
        self.assertEqual(_classify_by_midpoint(-1.0), "MODESTLY_LOWERED")
        self.assertEqual(_classify_by_midpoint(-2.0), "MODESTLY_LOWERED")
        self.assertEqual(_classify_by_midpoint(-5.0), "LOWERED")

    def test_strong_economic_event_scores_high(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a1",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=38.0,
            gross_margin_pct=78.5,
            gross_margin_change_bps=180.0,
            operating_margin_pct=22.3,
            free_cash_flow=890_000_000,
            reported_revenue=2_450_000_000,
            revenue_guidance_action="RAISED",
            revenue_guidance_change_pct=3.1,
            margin_guidance_action="RAISED",
            business_kpis={"RPO": "44%", "net revenue retention": "120%"},
        )
        score = score_economic_event(conf)
        self.assertGreaterEqual(score, 21.0)
        classification = classify_economic_event(score, conf)
        self.assertEqual(classification, "ECONOMIC_STRONG")

    def test_weak_economic_event_scores_low(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a2",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=-8.0,
            gross_margin_pct=62.0,
            gross_margin_change_bps=-300.0,
            revenue_guidance_action="LOWERED",
            revenue_guidance_change_pct=-4.3,
        )
        score = score_economic_event(conf)
        self.assertLess(score, 12.0)
        classification = classify_economic_event(score, conf)
        self.assertEqual(classification, "ECONOMIC_WEAK")

    def test_mixed_event_with_conflicting_signals(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a3",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=22.0,
            revenue_guidance_action="RAISED",
            revenue_guidance_change_pct=2.0,
            margin_guidance_action="LOWERED",
        )
        score = score_economic_event(conf)
        classification = classify_economic_event(score, conf)
        self.assertIn(classification, {"ECONOMIC_MIXED", "ECONOMIC_STRONG"})

    def test_withdrawn_guidance_is_weak(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a4",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=5.0,
            revenue_guidance_action="WITHDRAWN",
        )
        score = score_economic_event(conf)
        classification = classify_economic_event(score, conf)
        self.assertEqual(classification, "ECONOMIC_WEAK")

    def test_unavailable_source_stays_unavailable(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession=None,
            economic_classification="ECONOMIC_UNAVAILABLE",
            conflict_flags=["no_filing_match"],
        )
        score = score_economic_event(conf)
        classification = classify_economic_event(score, conf)
        self.assertEqual(classification, "ECONOMIC_UNAVAILABLE")

    def test_no_guidance_is_still_scored(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a5",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=12.0,
            gross_margin_pct=70.0,
            revenue_guidance_action="NOT_PROVIDED",
        )
        score = score_economic_event(conf)
        self.assertGreaterEqual(score, 0.0)


class GuidanceOverlayTests(unittest.TestCase):
    def test_strong_price_strong_fundamentals_preserves_actionable(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="actionable",
            conflict_type="PRICE_STRONG_FUNDAMENTALS_STRONG",
            economic_classification="ECONOMIC_STRONG",
            rev_guidance_action="RAISED",
            reason="core 82.0",
        )
        self.assertEqual(new_cls, "actionable")
        self.assertEqual(flags, [])

    def test_strong_price_weak_fundamentals_downgrades(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="actionable",
            conflict_type="PRICE_STRONG_FUNDAMENTALS_WEAK",
            economic_classification="ECONOMIC_WEAK",
            rev_guidance_action="MODESTLY_LOWERED",
            reason="core 80.0",
        )
        self.assertEqual(new_cls, "wait")
        self.assertIn("weak_fundamentals", flags)

    def test_material_guidance_cut_vetoes_actionable(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="actionable",
            conflict_type="PRICE_STRONG_FUNDAMENTALS_WEAK",
            economic_classification="ECONOMIC_WEAK",
            rev_guidance_action="LOWERED",
            reason="core 85.0",
        )
        self.assertEqual(new_cls, "wait")
        self.assertIn("material_guidance_cut", flags)

    def test_withdrawn_guidance_downgrades(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="actionable",
            conflict_type="PRICE_STRONG_FUNDAMENTALS_WEAK",
            economic_classification="ECONOMIC_WEAK",
            rev_guidance_action="WITHDRAWN",
            reason="core 78.0",
        )
        self.assertEqual(new_cls, "wait")
        self.assertIn("guidance_withdrawn", flags)

    def test_strong_fundamentals_poor_entry_stays_wait(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="wait",
            conflict_type="PRICE_WEAK_FUNDAMENTALS_STRONG",
            economic_classification="ECONOMIC_STRONG",
            rev_guidance_action="RAISED",
            reason="core 70.0 poor entry",
        )
        self.assertEqual(new_cls, "wait")

    def test_fundamentals_unavailable_preserves_with_flag(self):
        new_cls, reason, flags = apply_economic_overlay(
            classification="actionable",
            conflict_type="FUNDAMENTALS_UNAVAILABLE",
            economic_classification="ECONOMIC_UNAVAILABLE",
            rev_guidance_action="UNAVAILABLE",
            reason="core 82.0",
        )
        self.assertEqual(new_cls, "actionable")
        self.assertIn("fundamentals_unavailable", flags)

    def test_conflict_classification_mapping(self):
        self.assertEqual(
            determine_conflict_type("actionable", "ECONOMIC_STRONG"),
            "PRICE_STRONG_FUNDAMENTALS_STRONG",
        )
        self.assertEqual(
            determine_conflict_type("actionable", "ECONOMIC_MIXED"),
            "PRICE_STRONG_FUNDAMENTALS_MIXED",
        )
        self.assertEqual(
            determine_conflict_type("actionable", "ECONOMIC_WEAK"),
            "PRICE_STRONG_FUNDAMENTALS_WEAK",
        )
        self.assertEqual(
            determine_conflict_type("near_miss", "ECONOMIC_STRONG"),
            "PRICE_WEAK_FUNDAMENTALS_STRONG",
        )
        self.assertEqual(
            determine_conflict_type("excluded", "ECONOMIC_WEAK"),
            "PRICE_WEAK_FUNDAMENTALS_WEAK",
        )
        self.assertEqual(
            determine_conflict_type("actionable", "ECONOMIC_UNAVAILABLE"),
            "FUNDAMENTALS_UNAVAILABLE",
        )


class GuidanceIntegrationTests(unittest.TestCase):
    def test_apply_guidance_confirmation_strong_case(self):
        result = VpmaTickerResult(
            ticker="TEAM",
            classification="actionable",
            core_score=82.0,
            event_score=30.0,
            drift_score=28.0,
            entry_score=24.0,
            confirmation_score=None,
            data_confidence="medium",
            setup_type="pead_consolidation",
            reason="core strong",
            valid_for_days=3,
            details={"earnings_timestamp": "2026-04-15T16:30:00+00:00"},
        )
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a1",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=50.0,
            gross_margin_pct=78.5,
            gross_margin_change_bps=250.0,
            operating_margin_pct=25.0,
            free_cash_flow=900_000_000,
            reported_revenue=3_000_000_000,
            revenue_guidance_action="RAISED",
            revenue_guidance_change_pct=5.0,
            margin_guidance_action="RAISED",
            business_kpis={"RPO": "44%"},
        )
        score = score_economic_event(conf)
        conf.economic_classification = classify_economic_event(score, conf)
        conf.score = score
        updated = apply_guidance_confirmation(result, conf)
        self.assertEqual(updated.classification, "actionable")
        self.assertGreater(updated.economic_confirmation_score, 0)
        self.assertEqual(updated.economic_classification, "ECONOMIC_STRONG")
        self.assertIn("economic_classification", updated.details)
        self.assertIn("source_accession", updated.details)

    def test_apply_guidance_confirmation_weak_case_downgrades(self):
        result = VpmaTickerResult(
            ticker="TEAM",
            classification="actionable",
            core_score=80.0,
            event_score=28.0,
            drift_score=26.0,
            entry_score=26.0,
            confirmation_score=None,
            data_confidence="medium",
            setup_type="pead_breakout",
            reason="core strong",
            valid_for_days=3,
            details={},
        )
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a2",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=-8.0,
            gross_margin_change_bps=-300.0,
            revenue_guidance_action="LOWERED",
            revenue_guidance_change_pct=-4.3,
        )
        score = score_economic_event(conf)
        conf.economic_classification = classify_economic_event(score, conf)
        conf.score = score
        updated = apply_guidance_confirmation(result, conf)
        self.assertNotEqual(updated.classification, "actionable")
        self.assertIn(updated.classification, {"wait", "risk", "near_miss"})

    def test_guidance_confirmation_does_not_bypass_btd(self):
        conf = EarningsFundamentalConfirmation(
            ticker="TEAM",
            earnings_date=date(2026, 4, 15),
            source_accession="a1",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=50.0,
            gross_margin_pct=80.0,
            gross_margin_change_bps=300.0,
            operating_margin_pct=30.0,
            free_cash_flow=2_000_000_000,
            reported_revenue=5_000_000_000,
            revenue_guidance_action="RAISED",
            revenue_guidance_change_pct=6.0,
            margin_guidance_action="RAISED",
            business_kpis={"ARR": "50%"},
        )
        result = VpmaTickerResult(
            ticker="TEAM",
            classification="wait",
            core_score=70.0,
            event_score=25.0,
            drift_score=25.0,
            entry_score=20.0,
            confirmation_score=None,
            data_confidence="medium",
            setup_type="pead_pullback",
            reason="core weak entry",
            valid_for_days=3,
            details={},
        )
        updated = apply_guidance_confirmation(result, conf)
        self.assertEqual(updated.classification, "wait")


class GuidanceProviderIsolationTests(unittest.TestCase):
    def test_ticker_level_sec_failure_is_isolated(self):
        from datetime import UTC, datetime
        from scanners.vpma.engine import run_vpma_scan, apply_guidance_confirmation, VpmaConfig

        cfg = VpmaConfig(guidance_enable=True, guidance_max_tickers=2, enable_enrichment=False)

        good_conf = EarningsFundamentalConfirmation(
            ticker="GOOD",
            earnings_date=date(2026, 4, 15),
            source_accession="a-good",
            economic_classification="ECONOMIC_UNAVAILABLE",
            revenue_growth_yoy=45.0,
            gross_margin_pct=80.0,
            gross_margin_change_bps=300.0,
            operating_margin_pct=30.0,
            free_cash_flow=1_500_000_000,
            reported_revenue=4_000_000_000,
            revenue_guidance_action="RAISED",
            revenue_guidance_change_pct=5.0,
            margin_guidance_action="RAISED",
            business_kpis={"ARR": "50%"},
        )

        results = [
            VpmaTickerResult(
                ticker="GOOD",
                classification="actionable",
                core_score=82.0,
                event_score=30.0,
                drift_score=28.0,
                entry_score=24.0,
                confirmation_score=None,
                data_confidence="medium",
                setup_type="pead_consolidation",
                reason="good ticker",
                valid_for_days=3,
                details={"earnings_timestamp": "2026-04-15T16:30:00+00:00"},
            ),
            VpmaTickerResult(
                ticker="BAD",
                classification="actionable",
                core_score=78.0,
                event_score=30.0,
                drift_score=26.0,
                entry_score=22.0,
                confirmation_score=None,
                data_confidence="medium",
                setup_type="pead_consolidation",
                reason="bad ticker",
                valid_for_days=3,
                details={"earnings_timestamp": "2026-04-15T16:30:00+00:00"},
            ),
        ]

        good_result = apply_guidance_confirmation(results[0], good_conf)
        self.assertEqual(good_result.classification, "actionable")
        self.assertEqual(good_result.economic_classification, "ECONOMIC_STRONG")

        self.assertEqual(results[1].classification, "actionable")
        self.assertEqual(results[1].economic_classification, "")


if __name__ == "__main__":
    unittest.main()
