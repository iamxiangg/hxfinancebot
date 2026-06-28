from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from unittest.mock import patch

from providers.sec.models import FilingMetadata, SECInsiderTransaction
from scanners.insider.engine import (
    InsiderConfig,
    QualifyingPurchase,
    build_transaction_group_key,
    build_transaction_key,
    merge_purchase_history,
    parse_filing_purchases,
    run_insider_scan,
    score_cluster,
)
from scanners.insider.parser import NonDerivativeTransaction, ParsedOwnershipFiling, ReportingOwner


def make_purchase(
    *,
    accession: str,
    owner_cik: str,
    owner_name: str,
    owner_role: str,
    transaction_date: date,
    filing_date: date | None = None,
    shares: float = 10_000.0,
    price_per_share: float = 40.0,
    issuer_cik: str = "1",
    ticker: str = "TEAM",
    owner_is_operating: bool = True,
    owner_is_director: bool = False,
    owner_is_officer: bool = True,
    officer_title: str = "",
    direct_or_indirect: str = "D",
    confidence: str = "OPEN_MARKET_HIGH_CONFIDENCE",
    is_current_trigger: bool = False,
) -> QualifyingPurchase:
    transaction_group_key = build_transaction_group_key(
        issuer_cik=issuer_cik,
        owner_cik=owner_cik,
        transaction_date=transaction_date,
        security_title="Common Stock",
        direct_or_indirect=direct_or_indirect,
    )
    transaction_key = build_transaction_key(
        issuer_cik=issuer_cik,
        owner_cik=owner_cik,
        accession=accession,
        transaction_date=transaction_date,
        security_title="Common Stock",
        direct_or_indirect=direct_or_indirect,
        shares=shares,
        price_per_share=price_per_share,
    )
    return QualifyingPurchase(
        ticker=ticker,
        issuer_cik=issuer_cik,
        accession=accession,
        owner_cik=owner_cik,
        owner_name=owner_name,
        owner_role=owner_role,
        owner_is_operating=owner_is_operating,
        transaction_date=transaction_date,
        security_title="Common Stock",
        shares=shares,
        price_per_share=price_per_share,
        transaction_value=shares * price_per_share,
        direct_or_indirect=direct_or_indirect,
        plan_10b5_1=False,
        confidence=confidence,
        shares_owned_after=200_000.0,
        transaction_row_count=1,
        footnotes=[],
        owner_is_director=owner_is_director,
        owner_is_officer=owner_is_officer,
        officer_title=officer_title or owner_role,
        filing_date=filing_date or transaction_date,
        qualification_decision="QUALIFIED",
        qualification_reason=owner_role,
        observed_at="2026-06-27T00:00:00+00:00",
        transaction_key=transaction_key,
        transaction_group_key=transaction_group_key,
        source_fingerprint=transaction_key,
        is_current_trigger=is_current_trigger,
    )


class _FakeSECProvider:
    def __init__(self, day_to_filings: dict[date, list[FilingMetadata]], transactions_by_accession: dict[str, list[SECInsiderTransaction] | Exception]) -> None:
        self.day_to_filings = day_to_filings
        self.transactions_by_accession = transactions_by_accession

    def daily_index_filings(self, day: date, *, forms=None):
        if day not in self.day_to_filings:
            raise FileNotFoundError(str(day))
        filings = self.day_to_filings[day]
        if not forms:
            return filings
        forms_filter = {item.upper() for item in forms}
        return [filing for filing in filings if filing.form.upper() in forms_filter]

    def form4_transactions(self, filing: FilingMetadata) -> list[SECInsiderTransaction]:
        value = self.transactions_by_accession[filing.accession]
        if isinstance(value, Exception):
            raise value
        return value


class InsiderEngineTests(unittest.TestCase):
    def test_private_placement_purchase_is_excluded(self) -> None:
        filing = ParsedOwnershipFiling(
            accession="0001",
            issuer_cik="1",
            issuer_ticker="TEAM",
            acceptance_datetime="2026-06-20T00:00:00",
            reporting_owners=[
                ReportingOwner(
                    cik="10",
                    name="Jane Doe",
                    is_director=True,
                    is_officer=True,
                    is_ten_percent_owner=False,
                    officer_title="CEO",
                )
            ],
            transactions=[
                NonDerivativeTransaction(
                    security_title="Common Stock",
                    transaction_date="2026-06-20",
                    transaction_code="P",
                    acquired_disposed="A",
                    shares=1000,
                    price_per_share=10,
                    shares_owned_after=5000,
                    direct_or_indirect="D",
                    footnotes=["Private placement financing"],
                )
            ],
        )

        purchases, ledger_rows = parse_filing_purchases(
            filing,
            filing_date=date(2026, 6, 20),
            observed_at="2026-06-20T12:00:00+00:00",
        )

        self.assertEqual(purchases, [])
        self.assertEqual(ledger_rows[0]["decision"], "EXCLUDED")

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_ceo_cfo_cluster_can_be_actionable(self, _mock_market) -> None:
        purchases = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="CEO",
                owner_role="CEO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 21),
            ),
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="CFO",
                owner_role="CFO",
                transaction_date=date(2026, 6, 24),
                filing_date=date(2026, 6, 25),
                shares=15_000,
                price_per_share=42,
            ),
        ]

        result = score_cluster("TEAM", purchases, InsiderConfig())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn(result.classification, {"actionable", "wait"})
        self.assertEqual(result.unique_insiders, 2)
        self.assertGreaterEqual(result.total_score, 65.0)

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_cross_run_cluster_rehydrates_prior_purchase(self, _mock_market) -> None:
        historical = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="Director One",
                owner_role="Director",
                owner_is_operating=False,
                owner_is_director=True,
                owner_is_officer=False,
                transaction_date=date(2026, 6, 16),
                filing_date=date(2026, 6, 16),
            )
        ]
        current = [
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="CFO Two",
                owner_role="CFO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 20),
                shares=15_000,
                price_per_share=42.0,
                is_current_trigger=True,
            )
        ]

        merged, trigger_keys = merge_purchase_history(historical, current, since=date(2025, 6, 27))
        result = score_cluster("TEAM", merged, InsiderConfig(), current_trigger_group_keys=trigger_keys)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.unique_insiders, 2)
        self.assertIn("a1", result.source_accessions)
        self.assertIn("a2", result.source_accessions)

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_prior_purchase_outside_cluster_window_is_excluded(self, _mock_market) -> None:
        historical = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="Director One",
                owner_role="Director",
                owner_is_operating=False,
                owner_is_director=True,
                owner_is_officer=False,
                transaction_date=date(2026, 5, 1),
                filing_date=date(2026, 5, 2),
            )
        ]
        current = [
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="CFO Two",
                owner_role="CFO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 20),
                is_current_trigger=True,
            )
        ]
        merged, trigger_keys = merge_purchase_history(historical, current, since=date(2025, 6, 27))

        result = score_cluster("TEAM", merged, InsiderConfig(), current_trigger_group_keys=trigger_keys)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.unique_insiders, 1)
        self.assertEqual(result.source_accessions, ["a2"])

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_historical_cluster_without_new_trigger_is_not_reemitted(self, _mock_market) -> None:
        historical = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="CEO",
                owner_role="CEO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 20),
            ),
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="CFO",
                owner_role="CFO",
                transaction_date=date(2026, 6, 24),
                filing_date=date(2026, 6, 24),
            ),
        ]

        merged, trigger_keys = merge_purchase_history(historical, [], since=date(2025, 6, 27))
        result = score_cluster("TEAM", merged, InsiderConfig(), current_trigger_group_keys=trigger_keys)

        self.assertEqual(trigger_keys, set())
        self.assertIsNone(result)

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_form4a_correction_supersedes_prior_transaction(self, _mock_market) -> None:
        historical = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="CEO",
                owner_role="CEO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 20),
                shares=10_000,
                price_per_share=40.0,
            )
        ]
        current = [
            make_purchase(
                accession="a1-amend",
                owner_cik="10",
                owner_name="CEO",
                owner_role="CEO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 24),
                shares=12_000,
                price_per_share=39.5,
                is_current_trigger=True,
            ),
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="CFO",
                owner_role="CFO",
                transaction_date=date(2026, 6, 24),
                filing_date=date(2026, 6, 24),
                is_current_trigger=True,
            ),
        ]

        merged, trigger_keys = merge_purchase_history(historical, current, since=date(2025, 6, 27))
        ceo_purchase = next(item for item in merged if item.owner_cik == "10")
        result = score_cluster("TEAM", merged, InsiderConfig(), current_trigger_group_keys=trigger_keys)

        self.assertEqual(len([item for item in merged if item.owner_cik == "10"]), 1)
        self.assertEqual(ceo_purchase.accession, "a1-amend")
        self.assertEqual(ceo_purchase.shares, 12_000)
        self.assertIsNotNone(result)

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_unique_insiders_count_by_owner_cik_not_name(self, _mock_market) -> None:
        purchases = [
            make_purchase(
                accession="a1",
                owner_cik="10",
                owner_name="Same Name",
                owner_role="CEO",
                transaction_date=date(2026, 6, 20),
                filing_date=date(2026, 6, 20),
            ),
            make_purchase(
                accession="a2",
                owner_cik="11",
                owner_name="Same Name",
                owner_role="CFO",
                transaction_date=date(2026, 6, 24),
                filing_date=date(2026, 6, 24),
            ),
        ]

        result = score_cluster("TEAM", purchases, InsiderConfig())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.unique_insiders, 2)

    @patch("scanners.insider.engine._median_dollar_volume", return_value=(45.0, 20_000_000.0, []))
    def test_run_scan_isolates_filing_failures(self, _mock_market) -> None:
        target_day = date(2026, 6, 27)
        sec_provider = _FakeSECProvider(
            {
                target_day: [
                    FilingMetadata(
                        ticker="",
                        cik="0000000001",
                        accession="bad",
                        form="4",
                        filed_at=datetime(2026, 6, 27, tzinfo=UTC),
                        report_date=date(2026, 6, 27),
                        primary_document="bad.txt",
                        is_amendment=False,
                        source_url="https://www.sec.gov/Archives/edgar/data/1/bad.txt",
                    ),
                    FilingMetadata(
                        ticker="",
                        cik="0000000002",
                        accession="good",
                        form="4",
                        filed_at=datetime(2026, 6, 27, tzinfo=UTC),
                        report_date=date(2026, 6, 27),
                        primary_document="good.txt",
                        is_amendment=False,
                        source_url="https://www.sec.gov/Archives/edgar/data/2/good.txt",
                    ),
                ]
            },
            {
                "bad": RuntimeError("broken"),
                "good": [
                    SECInsiderTransaction(
                        ticker="TEAM",
                        issuer_cik="1",
                        accession="good",
                        owner_cik="10",
                        owner_name="CEO",
                        owner_is_director=False,
                        owner_is_officer=True,
                        owner_is_ten_percent_owner=False,
                        officer_title="CEO",
                        security_title="Common Stock",
                        transaction_date=date(2026, 6, 27),
                        transaction_code="P",
                        acquired_disposed="A",
                        shares=10_000,
                        price_per_share=40.0,
                        shares_owned_after=200_000.0,
                        direct_or_indirect="D",
                        footnotes=[],
                    ),
                    SECInsiderTransaction(
                        ticker="TEAM",
                        issuer_cik="1",
                        accession="good",
                        owner_cik="11",
                        owner_name="CFO",
                        owner_is_director=False,
                        owner_is_officer=True,
                        owner_is_ten_percent_owner=False,
                        officer_title="CFO",
                        security_title="Common Stock",
                        transaction_date=date(2026, 6, 27),
                        transaction_code="P",
                        acquired_disposed="A",
                        shares=10_000,
                        price_per_share=40.0,
                        shares_owned_after=200_000.0,
                        direct_or_indirect="D",
                        footnotes=[],
                    ),
                ],
            },
        )

        results, receipt = run_insider_scan(
            config=InsiderConfig(lookback_days=1),
            observed_at="2026-06-27T12:00:00+00:00",
            sec_provider=sec_provider,
        )

        self.assertEqual(receipt["scanned_entries"], 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ticker, "TEAM")


if __name__ == "__main__":
    unittest.main()
