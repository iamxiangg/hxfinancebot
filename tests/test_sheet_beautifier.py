"""Unit tests for funnel.sheet_beautifier filter-view logic.

Covers:
  - _resolve_header_indices maps known headers correctly, drops unknown.
  - _build_filter_view_request renders the correct Sheets addFilterView JSON
    with column indices resolved from header names.
  - Missing headers skip the spec rather than crash.
  - apply_filter_views is idempotent — re-running on a sheet that already
    has a matching view title issues zero batchUpdate requests.
  - apply_filter_views surfaces a structured _FilterViewApplyResult with
    applied / skipped_existing / skipped_missing_headers for receipt
    telemetry wiring in beautify_sheets.
  - _btd_filter_int_env parses env vars, falls back on garbage, falls back
    when unset.
  - beautify_sheets wires apply_filter_views for BTD_Candidates (we mock the
    Sheets service and assert the call).
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from funnel.review_schema import BTD_CANDIDATE_HEADERS, BTD_CANDIDATES_SHEET
from funnel.sheet_beautifier import (
    BTD_CANDIDATES_FILTER_VIEWS,
    _FilterSpec,
    _FilterViewSpec,
    _SortSpec,
    _btd_filter_int_env,
    _build_filter_view_request,
    _resolve_header_indices,
    apply_filter_views,
)


def _make_filter_view_service(existing_titles: list[str]) -> MagicMock:
    """Build a mock Sheets service whose ``spreadsheets().get().execute()``
    returns ``existing_titles`` as the filter views on sheet 5. Shared by the
    idempotency and missing-header tests below.
    """
    sheets_resource = MagicMock()
    sheets_resource.get.return_value.execute.return_value = {
        "sheets": [
            {
                "properties": {"sheetId": 5},
                "filterViews": [{"title": t} for t in existing_titles],
            }
        ],
    }
    spreadsheet_service = MagicMock()
    spreadsheet_service.spreadsheets.return_value = sheets_resource
    return spreadsheet_service


class TestResolveHeaderIndices(unittest.TestCase):
    def test_known_headers_resolve(self) -> None:
        headers = ["Ticker", "Status", "Congress Active Purchases"]
        result = _resolve_header_indices(
            headers, ["Ticker", "Status", "Congress Active Purchases"],
        )
        self.assertEqual(result, {"Ticker": 0, "Status": 1, "Congress Active Purchases": 2})

    def test_unknown_headers_dropped_silently(self) -> None:
        result = _resolve_header_indices(
            ["Ticker", "Status"], ["Ticker", "Congress Active Purchases"],
        )
        self.assertEqual(result, {"Ticker": 0})

    def test_strip_whitespace_in_header_names(self) -> None:
        result = _resolve_header_indices(["  Status  "], ["Status"])
        self.assertEqual(result, {"Status": 0})

    def test_returns_empty_when_no_overlap(self) -> None:
        result = _resolve_header_indices(["A"], ["B", "C"])
        self.assertEqual(result, {})


class TestBuildFilterViewRequest(unittest.TestCase):
    def test_renders_status_one_of_list(self) -> None:
        spec = _FilterViewSpec(
            name="V",
            filters=[
                _FilterSpec(header="Status", type="ONE_OF_LIST", values=["BTD_FAILED"]),
            ],
        )
        request, missing = _build_filter_view_request(
            spec, headers=["Status"], sheet_id=42, row_count=200,
        )
        self.assertIsNotNone(request)
        self.assertEqual(missing, [])
        view = request["addFilterView"]["filterView"]
        self.assertEqual(view["title"], "V")
        self.assertEqual(view["sortSpecs"], [])
        self.assertEqual(len(view["filterSpecs"]), 1)
        self.assertEqual(view["filterSpecs"][0]["columnIndex"], 0)
        condition = view["filterSpecs"][0]["filterCriteria"]["condition"]
        self.assertEqual(condition["type"], "ONE_OF_LIST")
        self.assertEqual(condition["values"], [{"userEnteredValue": "BTD_FAILED"}])

    def test_renders_number_gte_with_sort(self) -> None:
        spec = _FilterViewSpec(
            name="View",
            filters=[
                _FilterSpec(header="Congress Active Purchases", type="NUMBER_GREATER_THAN_EQ", values=["4"]),
                _FilterSpec(header="Congress Unique Members", type="NUMBER_GREATER_THAN_EQ", values=["2"]),
            ],
            sorts=[
                _SortSpec(header="Congress Active Purchases", descending=True),
                _SortSpec(header="Congress Unique Members", descending=True),
            ],
        )
        request, missing = _build_filter_view_request(
            spec,
            headers=["Congress Active Purchases", "Congress Unique Members"],
            sheet_id=99, row_count=2000,
        )
        self.assertIsNotNone(request)
        view = request["addFilterView"]["filterView"]
        self.assertEqual(view["sortSpecs"][0]["dimensionIndex"], 0)
        self.assertEqual(view["sortSpecs"][0]["sortOrder"], "DESCENDING")
        self.assertEqual(view["sortSpecs"][1]["dimensionIndex"], 1)
        self.assertEqual(view["sortSpecs"][1]["sortOrder"], "DESCENDING")
        self.assertEqual(view["range"]["sheetId"], 99)
        self.assertEqual(view["range"]["endRowIndex"], 2000)

    def test_returns_none_when_header_missing(self) -> None:
        spec = _FilterViewSpec(
            name="V",
            filters=[_FilterSpec(header="Status", type="ONE_OF_LIST", values=["X"])],
        )
        request, missing = _build_filter_view_request(
            spec, headers=["Ticker"], sheet_id=1, row_count=10,
        )
        self.assertIsNone(request)
        self.assertEqual(missing, ["Status"])

    def test_spec_matches_btd_candidates_real_headers(self) -> None:
        """The shipped view must resolve cleanly against the actual
        BTD_CANDIDATE_HEADERS list — guards against silent column-index
        mistakes if someone reorders the header list.
        """
        spec = BTD_CANDIDATES_FILTER_VIEWS[0]
        request, missing = _build_filter_view_request(
            spec,
            headers=BTD_CANDIDATE_HEADERS,
            sheet_id=1,
            row_count=2000,
        )
        self.assertIsNotNone(request, f"missing headers: {missing}")
        self.assertEqual(missing, [])

    def test_spec_simulating_header_rename_skips_without_crashing(self) -> None:
        """If a header is renamed in BTD_CANDIDATE_HEADERS (a common cause of
        silent column-index mistakes), ``_build_filter_view_request`` returns
        ``None`` and lists which headers are missing. ``apply_filter_views``
        is responsible for skipping + logging the spec rather than crashing.
        """
        spec = BTD_CANDIDATES_FILTER_VIEWS[0]
        mutated = [
            h for h in BTD_CANDIDATE_HEADERS
            if h not in ("Congress Active Purchases", "Congress Unique Members")
        ]
        request, missing = _build_filter_view_request(
            spec, headers=mutated, sheet_id=1, row_count=2000,
        )
        self.assertIsNone(request)
        self.assertIn("Congress Active Purchases", missing)
        self.assertIn("Congress Unique Members", missing)


class TestApplyFilterViewsIdempotent(unittest.TestCase):
    def test_skips_views_that_already_exist(self) -> None:
        service = _make_filter_view_service(
            existing_titles=["🚨 Strong Congress, BTD-Rejected"],
        )
        result = apply_filter_views(
            service,
            "fake-spreadsheet-id",
            sheet_name=BTD_CANDIDATES_SHEET,
            sheet_id=5,
            headers=BTD_CANDIDATE_HEADERS,
            row_count=2000,
            specs=BTD_CANDIDATES_FILTER_VIEWS,
        )
        self.assertEqual(result.applied, [])
        self.assertEqual(
            [s.name for s in result.skipped_existing],
            ["🚨 Strong Congress, BTD-Rejected"],
        )
        self.assertEqual(result.skipped_missing_headers, [])
        # Crucially, no batchUpdate should be issued if all views existed already.
        service.spreadsheets().batchUpdate.assert_not_called()

    def test_creates_views_that_do_not_yet_exist(self) -> None:
        service = _make_filter_view_service(existing_titles=[])
        result = apply_filter_views(
            service,
            "fake-spreadsheet-id",
            sheet_name=BTD_CANDIDATES_SHEET,
            sheet_id=5,
            headers=BTD_CANDIDATE_HEADERS,
            row_count=2000,
            specs=BTD_CANDIDATES_FILTER_VIEWS,
        )
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(result.skipped_existing, [])
        self.assertEqual(result.skipped_missing_headers, [])
        # Exactly one batchUpdate was issued with one addFilterView request.
        service.spreadsheets().batchUpdate.assert_called_once()
        kwargs = service.spreadsheets().batchUpdate.call_args.kwargs
        body = kwargs["body"]
        self.assertEqual(len(body["requests"]), 1)
        self.assertIn("addFilterView", body["requests"][0])
        view = body["requests"][0]["addFilterView"]["filterView"]
        self.assertEqual(view["title"], "🚨 Strong Congress, BTD-Rejected")

    def test_applies_only_missing_views_when_some_exist(self) -> None:
        # First run created TWO views; second run has one of them already.
        service = _make_filter_view_service(
            existing_titles=["Agent View (irrelevant to this test)"],
        )
        result = apply_filter_views(
            service,
            "fake-spreadsheet-id",
            sheet_name=BTD_CANDIDATES_SHEET,
            sheet_id=5,
            headers=BTD_CANDIDATE_HEADERS,
            row_count=2000,
            specs=BTD_CANDIDATES_FILTER_VIEWS,
        )
        # Our one view didn't exist in the existing set, so it gets created.
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(result.skipped_existing, [])
        service.spreadsheets().batchUpdate.assert_called_once()


class TestApplyFilterViewsMissingHeaders(unittest.TestCase):
    """When a required header is missing on the sheet (column renamed),
    the spec is reported in ``skipped_missing_headers`` with the missing
    column names, ``batchUpdate`` is NOT called for that spec, and the
    operator should investigate column drift via the receipt.
    """

    def test_missing_header_reported_in_skipped_list(self) -> None:
        service = _make_filter_view_service(existing_titles=[])
        # Drop two headers the spec depends on to simulate a rename.
        headers = [
            h for h in BTD_CANDIDATE_HEADERS
            if h not in ("Congress Active Purchases", "Congress Unique Members")
        ]
        spec = _FilterViewSpec(
            name="Strong congress review",
            filters=[
                _FilterSpec(header="Status", type="ONE_OF_LIST", values=["BTD_FAILED"]),
                _FilterSpec(
                    header="Congress Active Purchases",
                    type="NUMBER_GREATER_THAN_EQ",
                    values=["4"],
                ),
                _FilterSpec(
                    header="Congress Unique Members",
                    type="NUMBER_GREATER_THAN_EQ",
                    values=["2"],
                ),
            ],
        )
        result = apply_filter_views(
            service,
            "fake-spreadsheet-id",
            sheet_name=BTD_CANDIDATES_SHEET,
            sheet_id=5,
            headers=headers,
            row_count=2000,
            specs=[spec],
        )
        self.assertEqual(result.applied, [])
        self.assertEqual(result.skipped_existing, [])
        self.assertEqual(len(result.skipped_missing_headers), 1)
        name, missing = result.skipped_missing_headers[0]
        self.assertEqual(name, "Strong congress review")
        self.assertIn("Congress Active Purchases", missing)
        self.assertIn("Congress Unique Members", missing)
        service.spreadsheets().batchUpdate.assert_not_called()

    def test_partial_missing_header_still_reported(self) -> None:
        """If only one of the spec's referenced headers is missing, the spec
        is still skipped (a partial condition is unsafe to apply) and the
        single missing header is reported verbatim.
        """
        service = _make_filter_view_service(existing_titles=[])
        headers = [
            h for h in BTD_CANDIDATE_HEADERS
            if h != "Congress Active Purchases"
        ]
        spec = BTD_CANDIDATES_FILTER_VIEWS[0]  # has 3 filters
        result = apply_filter_views(
            service,
            "fake-spreadsheet-id",
            sheet_name=BTD_CANDIDATES_SHEET,
            sheet_id=5,
            headers=headers,
            row_count=2000,
            specs=[spec],
        )
        self.assertEqual(result.applied, [])
        self.assertEqual(len(result.skipped_missing_headers), 1)
        _, missing = result.skipped_missing_headers[0]
        self.assertEqual(missing, ["Congress Active Purchases"])


class TestBtdFilterIntEnv(unittest.TestCase):
    """``_btd_filter_int_env`` is the canonical reader for filter view
    thresholds. Operators can override ``BTD_FILTER_MIN_ACTIVE_PURCHASES`` and
    ``BTD_FILTER_MIN_UNIQUE_MEMBERS`` to tighten / loosen the filter. The
    helper must (a) parse valid integers, (b) fall back to the default on
    garbage (so a typo doesn't hard-crash the beautify), and (c) fall back to
    the default on unset.
    """

    def test_parses_valid_int(self) -> None:
        with patch.dict(os.environ, {"TEST_FILTER_MIN": "9"}):
            self.assertEqual(_btd_filter_int_env("TEST_FILTER_MIN", 4), 9)

    def test_falls_back_on_garbage(self) -> None:
        with patch.dict(os.environ, {"TEST_FILTER_MIN": "not-a-number"}):
            self.assertEqual(_btd_filter_int_env("TEST_FILTER_MIN", 4), 4)

    def test_uses_default_when_unset(self) -> None:
        # Explicitly clear any lingering value from other tests.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_FILTER_MIN", None)
            self.assertEqual(_btd_filter_int_env("TEST_FILTER_MIN", 4), 4)

    def test_zero_value_parses(self) -> None:
        # Setting threshold to 0 disables the numeric gate entirely.
        with patch.dict(os.environ, {"TEST_FILTER_MIN": "0"}):
            self.assertEqual(_btd_filter_int_env("TEST_FILTER_MIN", 4), 0)


class TestBeautifySheetsWiresFilterViews(unittest.TestCase):
    """Confirm ``beautify_sheets`` calls ``apply_filter_views`` ONLY for the
    BTD_Candidates sheet, with the correct arguments.
    """

    def test_btd_candidates_invokes_filter_views(self) -> None:
        from funnel.sheet_beautifier import beautify_sheets

        with patch("funnel.sheet_beautifier.apply_filter_views") as mock_apply:
            with patch("funnel.sheet_beautifier.apply_header_format"), \
                 patch("funnel.sheet_beautifier.apply_zebra_stripes"), \
                 patch("funnel.sheet_beautifier.apply_column_widths"), \
                 patch("funnel.sheet_beautifier.apply_number_formats"), \
                 patch("funnel.sheet_beautifier.apply_tab_colour"), \
                 patch(
                     "funnel.sheet_beautifier.get_sheets_service",
                     return_value=MagicMock(),
                 ), \
                 patch(
                     "funnel.sheet_beautifier.get_spreadsheet_id",
                     return_value="fake",
                 ), \
                 patch(
                     "funnel.sheet_beautifier._sheet_metadata",
                     return_value={
                         BTD_CANDIDATES_SHEET: {"sheetId": 7, "gridProperties": {"rowCount": 2000}},
                         "Other Sheet": {"sheetId": 8, "gridProperties": {"rowCount": 2000}},
                     },
                 ):
                beautify_sheets(
                    sheet_filter=[BTD_CANDIDATES_SHEET],
                )
        mock_apply.assert_called_once()
        kwargs = mock_apply.call_args.kwargs
        self.assertEqual(kwargs["sheet_name"], BTD_CANDIDATES_SHEET)
        self.assertEqual(kwargs["sheet_id"], 7)
        self.assertEqual(kwargs["specs"], BTD_CANDIDATES_FILTER_VIEWS)


if __name__ == "__main__":
    unittest.main()
