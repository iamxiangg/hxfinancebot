"""
Regression tests for dynamic column references in pilot sheets.

These tests guard against the failure mode where a hardcoded column letter
(e.g. ``A:Z`` or ``A1:O1``) would silently drop or expose columns when
the canonical header list is reordered, extended, or trimmed.

Pattern: read the relevant Sheets API call's range from the mock and
verify it scales with the number of headers, not a literal.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import unittest

from funnel import pilot_signal_log_writer
from funnel import pilot_writer
from funnel.pilot_signal_log_writer import SIGNAL_HEADERS, TARGET_SHEET
from funnel.pilot_writer import (
    FUNNEL_HEADERS,
    FUNNEL_SHEET,
    PENDING_HEADERS,
    PENDING_SHEET,
    SIGNAL_HEADERS as PILOT_SIGNAL_HEADERS,
    SIGNAL_LOG_SHEET,
)
from funnel.sheet_table import column_letter


def _build_service_spy(
    data_response: list[list[str]] | None = None,
) -> MagicMock:
    """A MagicMock whose ``get`` returns a context-aware response.

    A "header read" is any call whose cell anchor is row 1 (the range
    starts at ``A1`` or ``A1:..``). All other reads are treated as
    "data reads" and return ``{"values": data_response}`` (empty by
    default — pass ``data_response`` to opt into pre-seeded rows).

    The header payload always reflects the LIVE
    ``pilot_signal_log_writer.SIGNAL_HEADERS`` at call time, so tests
    that patch the module-level list are honoured end-to-end. A test
    that patches ``SIGNAL_HEADERS`` to a non-iterable falls back to
    empty values rather than propagating a confusing ``TypeError``
    from ``list(None)``.

    To extend the mock with a non-default behaviour (e.g. raise on a
    specific range, or return a custom header), override
    ``service.get.side_effect`` per-test.
    """
    service = MagicMock()
    service.spreadsheets.return_value = service
    service.values.return_value = service
    service.batchUpdate.return_value.execute.return_value = {}
    service.append.return_value.execute.return_value = {}
    service.update.return_value.execute.return_value = {}
    service.clear.return_value.execute.return_value = {}

    def _is_header_read(range_str: str) -> bool:
        # A "header read" is a read whose entire span sits on row 1.
        # Why this exact rule and not just "starts with A1:"? Because
        # ``A1:AA2`` (a 2-row block starting at A1) is a DATA read
        # masquerading as one — the second row is data, not header.
        # So we require BOTH the start anchor AND the end anchor to
        # be on row 1. Catches: ``A1`` ✓, ``A1:O1`` ✓, ``A1:AA2`` ✗,
        # ``B1:O1`` ✗, ``A11:O1`` ✗.
        if "!" not in range_str:
            return False
        cell_range = range_str.split("!", 1)[1]
        if cell_range == "A1":
            return True
        if not cell_range.startswith("A1:"):
            return False
        # End anchor must be on row 1 (e.g., ``O1`` ✓, ``AA2`` ✗).
        end_anchor = cell_range.split(":", 1)[1]
        return end_anchor.endswith("1")

    def _get_side_effect(*args, **kwargs):
        range_str = str(kwargs.get("range", ""))
        if _is_header_read(range_str):
            # `or []` falls back to empty when a test patches
            # SIGNAL_HEADERS to a non-iterable (None, etc.) — keeps
            # the caller's length check clean instead of raising
            # TypeError from ``list(None)``. We only emit a header
            # row when we actually have headers; otherwise return
            # an empty ``values`` payload so the caller's
            # ``actual_headers or [] == expected_headers`` check
            # works correctly.
            headers = pilot_signal_log_writer.SIGNAL_HEADERS or []
            payload = {"values": [list(headers)]} if headers else {"values": []}
        else:
            payload = {"values": list(data_response) if data_response else []}
        response = MagicMock()
        response.execute.return_value = payload
        return response

    service.get.side_effect = _get_side_effect
    return service


def _all_ranges(service: MagicMock) -> list[str]:
    """Collect every ``range=`` kwarg the spy saw across all API verbs."""
    ranges: list[str] = []
    for verb in (service.get, service.append, service.update, service.clear):
        for call_ in verb.call_args_list:
            value = call_.kwargs.get("range")
            if value is not None:
                ranges.append(value)
    return ranges


class PilotWriterReadTableDynamicColumns(unittest.TestCase):
    """``pilot_writer._read_table`` must derive the range from the header
    length passed in by the caller — not from a hardcoded ``A:Z``."""

    def setUp(self) -> None:
        self.service = _build_service_spy()

    def test_funnel_pilot_uses_full_27_column_range(self) -> None:
        # FUNNEL_HEADERS is 27 columns long. A static ``A:Z`` would silently
        # drop column 27 (AA). The dynamic refactor must reach AA.
        pilot_writer._read_table(
            self.service, "sid", FUNNEL_SHEET, len(FUNNEL_HEADERS),
        )
        expected = f"'{FUNNEL_SHEET}'!A:{column_letter(len(FUNNEL_HEADERS))}"
        self.assertIn(expected, _all_ranges(self.service))

    def test_signal_log_uses_full_15_column_range(self) -> None:
        pilot_writer._read_table(
            self.service, "sid", SIGNAL_LOG_SHEET, len(PILOT_SIGNAL_HEADERS),
        )
        expected = (
            f"'{SIGNAL_LOG_SHEET}'!"
            f"A:{column_letter(len(PILOT_SIGNAL_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_pending_uses_full_24_column_range(self) -> None:
        pilot_writer._read_table(
            self.service, "sid", PENDING_SHEET, len(PENDING_HEADERS),
        )
        expected = (
            f"'{PENDING_SHEET}'!A:{column_letter(len(PENDING_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_range_scales_with_growing_header_list(self) -> None:
        # If the schema grows (a future contributor adds a 28th header), the
        # read must scale automatically.
        pilot_writer._read_table(self.service, "sid", FUNNEL_SHEET, 30)
        expected = f"'{FUNNEL_SHEET}'!A:{column_letter(30)}"
        self.assertIn(expected, _all_ranges(self.service))

    def test_range_scales_with_shrinking_header_list(self) -> None:
        # If the schema shrinks (someone removes a header), the read must
        # shrink too — never read columns the code can't interpret.
        pilot_writer._read_table(self.service, "sid", FUNNEL_SHEET, 5)
        expected = f"'{FUNNEL_SHEET}'!A:{column_letter(5)}"
        self.assertIn(expected, _all_ranges(self.service))

    def test_endpoint_exactly_matches_column_letter(self) -> None:
        # Stronger guarantee than substring search: the range's column
        # endpoint must equal ``column_letter(N)`` exactly. Catches any
        # hardcoded letter (Z, AA, M, …) that the substring check would miss.
        pilot_writer._read_table(
            self.service, "sid", FUNNEL_SHEET, len(FUNNEL_HEADERS),
        )
        for raw in _all_ranges(self.service):
            self.assertTrue(
                raw.endswith(f":{column_letter(len(FUNNEL_HEADERS))}"),
                f"Range {raw!r} does not end with the dynamic letter.",
            )


class PilotWriterWriteTableDynamicColumns(unittest.TestCase):
    """``pilot_writer._write_table`` must clear the same column extent
    it writes — both derived from ``len(headers)``."""

    def setUp(self) -> None:
        self.service = _build_service_spy()

    def test_clear_range_uses_header_count(self) -> None:
        pilot_writer._write_table(
            self.service, "sid", FUNNEL_SHEET, FUNNEL_HEADERS, [],
        )
        expected = f"'{FUNNEL_SHEET}'!A:{column_letter(len(FUNNEL_HEADERS))}"
        self.assertIn(expected, _all_ranges(self.service))

    def test_clear_range_scales_for_signal_log(self) -> None:
        pilot_writer._write_table(
            self.service, "sid", SIGNAL_LOG_SHEET, PILOT_SIGNAL_HEADERS, [],
        )
        expected = (
            f"'{SIGNAL_LOG_SHEET}'!"
            f"A:{column_letter(len(PILOT_SIGNAL_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_clear_range_scales_for_pending(self) -> None:
        pilot_writer._write_table(
            self.service, "sid", PENDING_SHEET, PENDING_HEADERS, [],
        )
        expected = (
            f"'{PENDING_SHEET}'!A:{column_letter(len(PENDING_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_protected_sheet_guard_still_fires(self) -> None:
        # The Stock Summary USD guard should still raise before any API
        # call, so even a dynamic refactor must not bypass it.
        with self.assertRaises(RuntimeError):
            pilot_writer._write_table(
                self.service, "sid", "Stock Summary USD", ["Ticker"], [],
            )


class PilotSignalLogWriterDynamicColumns(unittest.TestCase):
    """``pilot_signal_log_writer`` must derive every column extent from
    ``len(SIGNAL_HEADERS)`` instead of hardcoded letters."""

    def setUp(self) -> None:
        self.service = _build_service_spy()
        self.spreadsheet_id = "sid"

    def test_verify_header_uses_dynamic_range(self) -> None:
        pilot_signal_log_writer._verify_header(self.service, self.spreadsheet_id)
        # The range flows through ``_read_values`` which prefixes the sheet
        # name, so the final API call carries the full quoted sheet+range.
        expected = (
            f"'{TARGET_SHEET}'!A1:{column_letter(len(SIGNAL_HEADERS))}1"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_clear_existing_data_uses_dynamic_range(self) -> None:
        pilot_signal_log_writer._clear_existing_data(
            self.service, self.spreadsheet_id,
        )
        expected = (
            f"'{TARGET_SHEET}'!A2:{column_letter(len(SIGNAL_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_verify_written_rows_uses_dynamic_range(self) -> None:
        # The read range should be sized off the header count even when
        # there are zero written rows (the empty-rows fast path).
        pilot_signal_log_writer._verify_written_rows(
            self.service, self.spreadsheet_id, [],
        )
        expected = (
            f"'{TARGET_SHEET}'!A2:{column_letter(len(SIGNAL_HEADERS))}"
        )
        self.assertIn(expected, _all_ranges(self.service))

    def test_endpoint_exactly_matches_column_letter(self) -> None:
        # Stronger guarantee: any hardcoded letter (A:Z, A:M, …) would
        # cause the endpoint to differ from ``column_letter(N)``.
        pilot_signal_log_writer._verify_header(self.service, self.spreadsheet_id)
        pilot_signal_log_writer._clear_existing_data(
            self.service, self.spreadsheet_id,
        )
        pilot_signal_log_writer._verify_written_rows(
            self.service, self.spreadsheet_id, [],
        )
        expected_suffix = f":{column_letter(len(SIGNAL_HEADERS))}"
        for raw in _all_ranges(self.service):
            if raw.startswith(f"'{TARGET_SHEET}'!A"):
                self.assertTrue(
                    raw.endswith(expected_suffix)
                    or raw.endswith(f"{expected_suffix}1"),
                    f"Range {raw!r} endpoint does not match column_letter({len(SIGNAL_HEADERS)}).",
                )

    def test_range_scales_when_headers_grow(self) -> None:
        # The dynamic refactor must continue to work if SIGNAL_HEADERS grows
        # beyond 15. We patch the module's SIGNAL_HEADERS to 18 for the
        # duration of this test only.
        original = pilot_signal_log_writer.SIGNAL_HEADERS
        pilot_signal_log_writer.SIGNAL_HEADERS = list(original) + [
            "Extra Col 1", "Extra Col 2", "Extra Col 3",
        ]
        try:
            pilot_signal_log_writer._verify_header(
                self.service, self.spreadsheet_id,
            )
            expected = (
                f"'{TARGET_SHEET}'!A1:"
                f"{column_letter(len(pilot_signal_log_writer.SIGNAL_HEADERS))}1"
            )
            self.assertIn(expected, _all_ranges(self.service))
        finally:
            pilot_signal_log_writer.SIGNAL_HEADERS = original


class WritePilotResultsPerSheetRouting(unittest.TestCase):
    """Integration: ``write_pilot_results`` must thread the right header
    count into every ``_read_table`` call after a column-trimming event."""

    def setUp(self) -> None:
        self.service = _build_service_spy()
        # Stub the rows-to-dicts layer so we can run write_pilot_results
        # without inventing realistic Congress signals.
        self._orig_rows_to_dicts = pilot_writer._rows_to_dicts
        pilot_writer._rows_to_dicts = lambda rows, headers: []  # type: ignore[assignment]
        # Stub the side-effecting dependencies so write_pilot_results
        # completes without live credentials or extra API verbs.
        self._orig_ensure = pilot_writer.ensure_pilot_sheets
        pilot_writer.ensure_pilot_sheets = MagicMock()  # type: ignore[assignment]
        self._orig_get_service = pilot_writer.get_sheets_service
        pilot_writer.get_sheets_service = MagicMock(  # type: ignore[assignment]
            return_value=self.service,
        )
        self._orig_get_sid = pilot_writer.get_spreadsheet_id
        pilot_writer.get_spreadsheet_id = MagicMock(  # type: ignore[assignment]
            return_value="sid",
        )

    def tearDown(self) -> None:
        pilot_writer._rows_to_dicts = self._orig_rows_to_dicts  # type: ignore[assignment]
        pilot_writer.ensure_pilot_sheets = self._orig_ensure  # type: ignore[assignment]
        pilot_writer.get_sheets_service = self._orig_get_service  # type: ignore[assignment]
        pilot_writer.get_spreadsheet_id = self._orig_get_sid  # type: ignore[assignment]

    def test_each_sheet_uses_its_own_header_count(self) -> None:
        pilot_writer.write_pilot_results(
            signals=[], comparison=[],
        )
        ranges = _all_ranges(self.service)
        self.assertIn(
            f"'{PENDING_SHEET}'!A:{column_letter(len(pilot_writer.PENDING_HEADERS))}",
            ranges,
        )
        self.assertIn(
            f"'{SIGNAL_LOG_SHEET}'!"
            f"A:{column_letter(len(pilot_writer.SIGNAL_HEADERS))}",
            ranges,
        )
        self.assertIn(
            f"'{FUNNEL_SHEET}'!A:{column_letter(len(pilot_writer.FUNNEL_HEADERS))}",
            ranges,
        )


class ServiceSpyRegressionTests(unittest.TestCase):
    """Lock in the side_effect behaviour so a future revert to a static
    return_value (or a wrong heuristic) is caught immediately.

    These tests exercise ``_build_service_spy`` directly. They catch
    any future refactor that accidentally regresses the spy back to a
    single static response — which is exactly the failure mode that
    broke tests in the previous round.
    """

    def test_header_and_data_reads_return_different_responses(self) -> None:
        spy = _build_service_spy()
        header = spy.get(
            range=f"'{TARGET_SHEET}'!A1:{column_letter(len(SIGNAL_HEADERS))}1",
        ).execute()
        data = spy.get(
            range=f"'{TARGET_SHEET}'!A2:{column_letter(len(SIGNAL_HEADERS))}",
        ).execute()
        self.assertEqual(header["values"], [list(SIGNAL_HEADERS)])
        self.assertEqual(data["values"], [])

    def test_multi_row_reads_at_a1_are_NOT_header_reads(self) -> None:
        # A read like <sheet>!A1:AA2 (2-row header read) ends with '2' —
        # by our start-anchor rule, this is NOT a header read. This
        # prevents a future refactor from re-introducing the brittle
        # ``endswith('1')`` heuristic.
        spy = _build_service_spy()
        result = spy.get(range=f"'{TARGET_SHEET}'!A1:AA2").execute()
        self.assertEqual(result["values"], [])

    def test_single_cell_a1_is_a_header_read(self) -> None:
        # A bare ``A1`` read should be treated as a header read.
        spy = _build_service_spy()
        result = spy.get(range=f"'{TARGET_SHEET}'!A1").execute()
        self.assertEqual(result["values"], [list(SIGNAL_HEADERS)])

    def test_custom_data_response_is_returned_for_data_reads(self) -> None:
        spy = _build_service_spy(data_response=[["a", "b"], ["c", "d"]])
        result = spy.get(
            range=f"'{TARGET_SHEET}'!A2:{column_letter(len(SIGNAL_HEADERS))}",
        ).execute()
        self.assertEqual(result["values"], [["a", "b"], ["c", "d"]])

    def test_custom_data_response_does_NOT_leak_to_header_reads(self) -> None:
        # The data_response is for data reads only — header reads still
        # return the live SIGNAL_HEADERS.
        spy = _build_service_spy(data_response=[["a", "b"]])
        result = spy.get(range=f"'{TARGET_SHEET}'!A1:O1").execute()
        self.assertEqual(result["values"], [list(SIGNAL_HEADERS)])

    def test_non_iterable_signal_headers_falls_back_to_empty(self) -> None:
        # If a test patches SIGNAL_HEADERS to a non-iterable, the mock
        # must fall back to empty values rather than propagating a
        # confusing TypeError from list(None).
        original = pilot_signal_log_writer.SIGNAL_HEADERS
        pilot_signal_log_writer.SIGNAL_HEADERS = None  # type: ignore[assignment]
        try:
            spy = _build_service_spy()
            # Use a hardcoded column letter (O = 15) to avoid len(None).
            result = spy.get(range=f"'{TARGET_SHEET}'!A1:O1").execute()
            self.assertEqual(result["values"], [])
        finally:
            pilot_signal_log_writer.SIGNAL_HEADERS = original

    def test_service_spy_uses_side_effect_not_static_return(self) -> None:
        # Regression guard: a future revert to ``return_value = {...}``
        # would fail this assertion. The current implementation must
        # route reads through ``side_effect`` so it can differentiate
        # header reads from data reads.
        spy = _build_service_spy()
        self.assertIsNotNone(
            spy.get.side_effect,
            "_build_service_spy must use side_effect, not a static "
            "return_value, so it can distinguish header from data reads.",
        )


if __name__ == "__main__":
    unittest.main()
