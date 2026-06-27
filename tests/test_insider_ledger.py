from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from funnel.insider_ledger import load_qualified_purchases, persist_ledger_rows


class InsiderLedgerTests(unittest.TestCase):
    def test_local_json_history_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rows_path = Path(temp_dir) / "ledger_rows.json"
            rows_path.write_text(
                json.dumps(
                    [
                        {
                            "Decision": "QUALIFIED",
                            "Ticker": "TEAM",
                            "Issuer CIK": "1",
                            "Accession": "a1",
                            "Owner CIK": "10",
                            "Owner Name": "CEO",
                            "Owner Role": "CEO",
                            "Transaction Date": "2026-06-20",
                            "Security Title": "Common Stock",
                            "Shares": "10000",
                            "Price Per Share": "40",
                            "Transaction Value": "400000",
                            "Direct Or Indirect": "D",
                            "Confidence": "OPEN_MARKET_HIGH_CONFIDENCE",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch("funnel.insider_ledger._sheet_backend_enabled", return_value=False), patch(
                "funnel.insider_ledger._rows_path", return_value=rows_path
            ):
                purchases = load_qualified_purchases(since=date(2026, 1, 1))

        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].ticker, "TEAM")

    @patch("funnel.insider_ledger.ensure_review_sheets")
    @patch("funnel.insider_ledger.get_spreadsheet_id", return_value="sheet-id")
    @patch("funnel.insider_ledger.get_sheets_service", return_value=object())
    @patch(
        "funnel.insider_ledger.read_table",
        return_value=[
            {
                "Decision": "QUALIFIED",
                "Ticker": "TEAM",
                "Issuer CIK": "1",
                "Accession": "a1",
                "Owner CIK": "10",
                "Owner Name": "CEO",
                "Owner Role": "CEO",
                "Transaction Date": "2026-06-20",
                "Security Title": "Common Stock",
                "Shares": "10000",
                "Price Per Share": "40",
                "Transaction Value": "400000",
                "Direct Or Indirect": "D",
                "Confidence": "OPEN_MARKET_HIGH_CONFIDENCE",
            }
        ],
    )
    def test_google_sheets_history_loading_through_mocks(
        self,
        mock_read,
        mock_service,
        mock_sheet_id,
        mock_ensure,
    ) -> None:
        with patch("funnel.insider_ledger._sheet_backend_enabled", return_value=True):
            purchases = load_qualified_purchases(since=date(2026, 1, 1))

        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].owner_role, "CEO")
        mock_read.assert_called_once()
        mock_ensure.assert_called_once()

    def test_legacy_rows_with_missing_new_columns_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rows_path = Path(temp_dir) / "ledger_rows.json"
            rows_path.write_text(
                json.dumps(
                    [
                        {
                            "Accession": "a1",
                            "Ticker": "TEAM",
                            "Owner CIK": "10",
                            "Owner Name": "CEO",
                            "Transaction Date": "2026-06-20",
                            "Security Title": "Common Stock",
                            "Shares": "10000",
                            "Price Per Share": "40",
                            "Transaction Value": "400000",
                            "Direct Or Indirect": "D",
                            "Decision": "QUALIFIED",
                            "Reason": "CEO",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch("funnel.insider_ledger._sheet_backend_enabled", return_value=False), patch(
                "funnel.insider_ledger._rows_path", return_value=rows_path
            ):
                purchases = load_qualified_purchases(since=date(2026, 1, 1))

        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].confidence, "OPEN_MARKET_MEDIUM_CONFIDENCE")
        self.assertEqual(purchases[0].owner_role, "CEO")

    def test_rejected_private_award_rows_do_not_rehydrate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rows_path = Path(temp_dir) / "ledger_rows.json"
            rows_path.write_text(
                json.dumps(
                    [
                        {
                            "Decision": "EXCLUDED",
                            "Ticker": "TEAM",
                            "Accession": "bad1",
                            "Reason": "excluded_context",
                            "Transaction Date": "2026-06-20",
                        },
                        {
                            "Decision": "QUALIFIED",
                            "Ticker": "TEAM",
                            "Accession": "good1",
                            "Owner CIK": "10",
                            "Owner Name": "CEO",
                            "Owner Role": "CEO",
                            "Transaction Date": "2026-06-20",
                            "Security Title": "Common Stock",
                            "Shares": "10000",
                            "Price Per Share": "40",
                            "Transaction Value": "400000",
                            "Direct Or Indirect": "D",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            with patch("funnel.insider_ledger._sheet_backend_enabled", return_value=False), patch(
                "funnel.insider_ledger._rows_path", return_value=rows_path
            ):
                purchases = load_qualified_purchases(since=date(2026, 1, 1))

        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].accession, "good1")

    def test_duplicate_and_amended_rows_do_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rows_path = Path(temp_dir) / "ledger_rows.json"
            with patch("funnel.insider_ledger._sheet_backend_enabled", return_value=False), patch(
                "funnel.insider_ledger._rows_path", return_value=rows_path
            ):
                persist_ledger_rows(
                    [
                        {
                            "transaction_key": "txn-old",
                            "transaction_group_key": "grp-1",
                            "accession": "a1",
                            "issuer_cik": "1",
                            "ticker": "TEAM",
                            "owner_cik": "10",
                            "owner_name": "CEO",
                            "owner_role": "CEO",
                            "transaction_date": "2026-06-20",
                            "filing_date": "2026-06-20",
                            "security_title": "Common Stock",
                            "shares": 10000,
                            "price_per_share": 40,
                            "transaction_value": 400000,
                            "direct_or_indirect": "D",
                            "decision": "QUALIFIED",
                            "qualification_decision": "QUALIFIED",
                            "reason": "CEO",
                            "qualification_reason": "CEO",
                        },
                        {
                            "transaction_key": "txn-old",
                            "transaction_group_key": "grp-1",
                            "accession": "a1",
                            "issuer_cik": "1",
                            "ticker": "TEAM",
                            "owner_cik": "10",
                            "owner_name": "CEO",
                            "owner_role": "CEO",
                            "transaction_date": "2026-06-20",
                            "filing_date": "2026-06-20",
                            "security_title": "Common Stock",
                            "shares": 10000,
                            "price_per_share": 40,
                            "transaction_value": 400000,
                            "direct_or_indirect": "D",
                            "decision": "QUALIFIED",
                            "qualification_decision": "QUALIFIED",
                            "reason": "CEO",
                            "qualification_reason": "CEO",
                        },
                        {
                            "transaction_key": "txn-amend",
                            "transaction_group_key": "grp-1",
                            "accession": "a1-amend",
                            "issuer_cik": "1",
                            "ticker": "TEAM",
                            "owner_cik": "10",
                            "owner_name": "CEO",
                            "owner_role": "CEO",
                            "transaction_date": "2026-06-20",
                            "filing_date": "2026-06-24",
                            "security_title": "Common Stock",
                            "shares": 12000,
                            "price_per_share": 39.5,
                            "transaction_value": 474000,
                            "direct_or_indirect": "D",
                            "decision": "QUALIFIED",
                            "qualification_decision": "QUALIFIED",
                            "reason": "CEO",
                            "qualification_reason": "CEO",
                        },
                    ],
                    observed_at="2026-06-24T12:00:00+00:00",
                )
                purchases = load_qualified_purchases(since=date(2026, 1, 1))
                stored_rows = json.loads(rows_path.read_text(encoding="utf-8"))

        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].accession, "a1-amend")
        superseded = [row for row in stored_rows if row.get("Superseded By")]
        self.assertEqual(len(superseded), 1)


if __name__ == "__main__":
    unittest.main()
