from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from funnel.sheet_table import ensure_sheets, upsert_records_from_loaded_rows


class SheetTableTests(unittest.TestCase):
    def test_ensure_sheets_batches_header_reads(self) -> None:
        service = MagicMock()
        spreadsheets = service.spreadsheets.return_value
        values = spreadsheets.values.return_value
        spreadsheets.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": "First"}},
                {"properties": {"title": "Second"}},
            ]
        }
        values.batchGet.return_value.execute.return_value = {
            "valueRanges": [
                {"values": [["Key", "Value"]]},
                {"values": [["Trade Key"]]},
            ]
        }

        ensure_sheets(
            service,
            "spreadsheet-id",
            [
                ("First", ["Key", "Value"]),
                ("Second", ["Trade Key"]),
            ],
        )

        self.assertEqual(spreadsheets.get.call_count, 1)
        self.assertEqual(values.batchGet.call_count, 1)
        values.get.assert_not_called()
        values.batchUpdate.assert_not_called()

    def test_upsert_from_loaded_rows_updates_without_another_read(self) -> None:
        service = MagicMock()
        values = service.spreadsheets.return_value.values.return_value
        values.append.return_value.execute.return_value = {
            "updates": {"updatedRange": "'Political_Trades_Raw'!A9:B9"}
        }
        row_by_key = {"KNOWN": 4}

        upsert_records_from_loaded_rows(
            service,
            "spreadsheet-id",
            "Political_Trades_Raw",
            ["Trade Key", "Value"],
            "Trade Key",
            [
                {"Trade Key": "known", "Value": "updated"},
                {"Trade Key": "new", "Value": "inserted"},
            ],
            row_by_key,
        )

        values.get.assert_not_called()
        update_body = values.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(update_body["data"][0]["range"], "'Political_Trades_Raw'!A4:B4")
        self.assertEqual(row_by_key, {"KNOWN": 4, "NEW": 9})

