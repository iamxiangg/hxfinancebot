"""
Sheet Beautifier — one-shot formatting for all production Google Sheets.

Applies:
  - Bold, wrapped headers with frozen row 1
  - Alternating row colours (zebra stripes) from row 2
  - Auto-resized column widths
  - Number formatting (currency, percentage, date, integer) per column type
  - Colour-coded sheet tabs

Usage:
  python -m funnel.sheet_beautifier              # all production sheets
  python -m funnel.sheet_beautifier --dry-run    # preview without applying
  BEAUTIFY_SHEETS="Stock Summary USD,BTD_Candidates" python -m funnel.sheet_beautifier
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from funnel.google_client import get_sheets_service, get_spreadsheet_id
from funnel.review_schema import (
    BOT_STATE_HEADERS,
    BOT_STATE_SHEET,
    BTD_CANDIDATE_HEADERS,
    BTD_CANDIDATES_SHEET,
    CONGRESS_LEDGER_HEADERS,
    CONGRESS_LEDGER_SHEET,
    DECISION_LOG_HEADERS,
    DECISION_LOG_SHEET,
    FEROLDI_AI_DRAFT_HEADERS,
    FEROLDI_AI_DRAFTS_SHEET,
    INSIDER_LEDGER_HEADERS,
    INSIDER_LEDGER_SHEET,
    MASTERLIST_SHEET,
    MANUAL_SEED_HEADERS,
    MANUAL_SEED_SHEET,
    SIGNAL_LOG_HEADERS,
    SIGNAL_LOG_SHEET,
)
from funnel.sheet_table import column_letter as _column_letter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
REVIEW_REQUESTS_SHEET = "Review_Requests"

TAB_COLOURS: dict[str, dict[str, float]] = {
    MASTERLIST_SHEET: {"red": 0.051, "green": 0.502, "blue": 0.263},
    SIGNAL_LOG_SHEET: {"red": 0.102, "green": 0.451, "blue": 0.910},
    BTD_CANDIDATES_SHEET: {"red": 0.976, "green": 0.671, "blue": 0.0},
    REVIEW_REQUESTS_SHEET: {"red": 0.890, "green": 0.455, "blue": 0.0},
    FEROLDI_AI_DRAFTS_SHEET: {"red": 0.576, "green": 0.204, "blue": 0.894},
    BOT_STATE_SHEET: {"red": 0.373, "green": 0.380, "blue": 0.408},
    DECISION_LOG_SHEET: {"red": 0.071, "green": 0.620, "blue": 0.686},
    CONGRESS_LEDGER_SHEET: {"red": 0.851, "green": 0.188, "blue": 0.145},
    INSIDER_LEDGER_SHEET: {"red": 0.773, "green": 0.133, "blue": 0.122},
    MANUAL_SEED_SHEET: {"red": 0.482, "green": 0.545, "blue": 0.227},
}

ZEBRA_FIRST: dict[str, float] = {"red": 1.0, "green": 1.0, "blue": 1.0}
ZEBRA_SECOND: dict[str, float] = {"red": 0.953, "green": 0.957, "blue": 0.965}

# ---------------------------------------------------------------------------
# Production sheets — (name, headers) tuples
# ---------------------------------------------------------------------------
PRODUCTION_SHEETS: list[tuple[str, list[str]]] = [
    (MASTERLIST_SHEET, []),
    (SIGNAL_LOG_SHEET, SIGNAL_LOG_HEADERS),
    (BTD_CANDIDATES_SHEET, BTD_CANDIDATE_HEADERS),
    (REVIEW_REQUESTS_SHEET, []),
    (FEROLDI_AI_DRAFTS_SHEET, FEROLDI_AI_DRAFT_HEADERS),
    (BOT_STATE_SHEET, BOT_STATE_HEADERS),
    (DECISION_LOG_SHEET, DECISION_LOG_HEADERS),
    (CONGRESS_LEDGER_SHEET, CONGRESS_LEDGER_HEADERS),
    (INSIDER_LEDGER_SHEET, INSIDER_LEDGER_HEADERS),
    (MANUAL_SEED_SHEET, MANUAL_SEED_HEADERS),
]


# ---------------------------------------------------------------------------
# Saved filter views
# ---------------------------------------------------------------------------
# NamedFilterView definitions. Each entry references headers by name so that
# column-index lookups remain robust if BTD_CANDIDATE_HEADERS is reordered or
# extended. If a referenced header is missing, that specific spec is skipped
# with a warning rather than failing the whole beautify run.
#
# Sheets API reference (filter views, NOT basic filter):
#   addFilterView -> filter {title, range, sortSpecs, filterSpecs}
#   filterSpec.columnIndex + filterCriteria.condition (type ∈ ConditionType
#   enum: TEXT_EQ, TEXT_CONTAINS, NUMBER_GREATER_THAN_EQ,
#   NUMBER_LESS_THAN_EQ, CUSTOM_FORMULA, ...).
#
# Multi-value OR matching: ``ONE_OF_LIST`` is for data-validation rules, not
# filter views, and is rejected with ``ConditionType 'ONE_OF_LIST' is not
# supported in filters.``. ``TEXT_EQ`` does not give clean OR semantics
# across multiple values either. The reliable path is ``CUSTOM_FORMULA``
# with an ``=OR(...)`` formula. The first value in ``_FilterSpec.values``
# is treated as a format-string template and supports two placeholders:
#   ``{col}``  — substituted with the column letter of the resolved header
#   ``{row}``  — substituted with the first data row (currently 2; the
#               header sits on row 1, data starts on row 2)

FilterConditionType = str  # alias for readability

# First data row (1-based) is 2 because the header sits on row 1 and data
# starts on row 2. ``CUSTOM_FORMULA`` templates use ``{row}`` as a
# placeholder; the substitution lives in ``_build_filter_view_request``.
# Promoting to a module-level constant keeps the layout assumption
# grep-able and gives a future override path an obvious handle.
_FIRST_DATA_ROW = 2


@dataclass
class _FilterSpec:
    header: str
    """Header name used to resolve the column index at runtime."""
    type: FilterConditionType
    values: list[str]


@dataclass
class _SortSpec:
    header: str
    descending: bool = False


@dataclass
class _FilterViewSpec:
    name: str
    filters: list[_FilterSpec] = field(default_factory=list)
    sorts: list[_SortSpec] = field(default_factory=list)
    """Human-readable explanation dumped into the receipt on dry-run so the
    operator can confirm the view's intent without reading the source."""


# Filter thresholds are env-overridable so operators can tune them without
# code edits. ``_btd_filter_int_env`` is the canonical reader: it returns the
# default if the env var is unset OR unparseable. The beautify never crashes
# on a bad value because the filter view is a non-destructive aid, not a hard
# gate.
def _btd_filter_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Filter view threshold %s=%r is not an int — using default %d.",
            name, raw, default,
        )
        return default


# "Strong Congress, BTD-Rejected" — surfaces the INTC-style pattern the user
# flagged: ticker rejected by the BTD gate, but the congress-signal footprint
# is too strong to ignore. Operators open BTD_Candidates, switch to this view,
# and triage.
BTD_CANDIDATES_FILTER_VIEWS: list[_FilterViewSpec] = [
    _FilterViewSpec(
        name="🚨 Strong Congress, BTD-Rejected",
        filters=[
            # Multi-value OR via CUSTOM_FORMULA — Sheets API's filter views do
            # not accept ONE_OF_LIST, and TEXT_EQ does not give clean OR
            # semantics across multiple values. The formula references the
            # resolved column letter via ``{col}`` and the first data row
            # via ``{row}`` (both substituted at request-build time, see
            # _build_filter_view_request). Row 2 is the convention: the
            # filter view's data range starts at the second 1-based row
            # because row 1 holds the header.
            _FilterSpec(
                header="Status",
                type="CUSTOM_FORMULA",
                values=[
                    '=OR({col}{row}="BTD_FAILED",{col}{row}="BTD_UNAVAILABLE")',
                ],
            ),
            _FilterSpec(
                header="Congress Active Purchases",
                type="NUMBER_GREATER_THAN_EQ",
                values=[
                    str(_btd_filter_int_env("BTD_FILTER_MIN_ACTIVE_PURCHASES", 4))
                ],
            ),
            _FilterSpec(
                header="Congress Unique Members",
                type="NUMBER_GREATER_THAN_EQ",
                values=[
                    str(_btd_filter_int_env("BTD_FILTER_MIN_UNIQUE_MEMBERS", 2))
                ],
            ),
        ],
        sorts=[
            _SortSpec(header="Congress Active Purchases", descending=True),
            _SortSpec(header="Congress Unique Members", descending=True),
        ],
    ),
]


# Sheet name → filter views to apply on that sheet. Future sheets add an entry
# here without touching the wiring block in ``beautify_sheets``. An absent key
# (or empty list) means "no filter views for this sheet" — the call is skipped
# entirely, so this stays cheap for sheets that don't need it.
FILTER_VIEW_CONFIG: dict[str, list[_FilterViewSpec]] = {
    BTD_CANDIDATES_SHEET: BTD_CANDIDATES_FILTER_VIEWS,
}


def _resolve_header_indices(
    headers: list[str],
    needed: list[str],
) -> dict[str, int]:
    """Build a ``{header_name: column_index}`` map. Missing headers are
    silently dropped; callers can inspect the missing set via the unfulfilled
    argument outside this helper."""
    by = {h.strip(): idx for idx, h in enumerate(headers)}
    out: dict[str, int] = {}
    for name in needed:
        if name in by:
            out[name] = by[name]
    return out


def _build_filter_view_request(
    spec: _FilterViewSpec,
    *,
    headers: list[str],
    sheet_id: int,
    row_count: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Render a single ``_FilterViewSpec`` to the Sheets ``addFilterView``
    request body shape, or return ``None`` if any required header is missing.

    Returns ``(request_or_none, missing_headers)``. The caller decides
    whether to log+skip or hard-fail on missing headers.
    """
    needed = [f.header for f in spec.filters] + [s.header for s in spec.sorts]
    resolved = _resolve_header_indices(headers, needed)
    missing = sorted(set(needed) - set(resolved))
    if missing:
        return None, missing

    column_count = len(headers)

    filter_specs: list[dict[str, Any]] = []
    for f in spec.filters:
        col_letter = _column_letter(resolved[f.header] + 1)
        if f.type == "CUSTOM_FORMULA":
            # Treat the first value as a format-string template; substitute
            # ``{col}`` with the resolved column letter and ``{row}`` with
            # the first data row. Used to express multi-value OR matches
            # that the Sheets API does not otherwise support for filter
            # views (e.g., status in a set of values).
            if not f.values:
                # Defensive: a CUSTOM_FORMULA with no template is malformed.
                # Returned as a missing-header so the caller logs+skips
                # the spec rather than shipping a broken request. An empty
                # ``values`` list is NOT a "filter shows everything" intent;
                # if a future caller actually wants an unfiltered view,
                # they should omit the spec from the FilterViewSpec.
                return None, [f.header]
            formula = f.values[0].format(col=col_letter, row=_FIRST_DATA_ROW)
            condition: dict[str, Any] = {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": formula}],
            }
        else:
            condition = {
                "type": f.type,
                "values": [{"userEnteredValue": v} for v in f.values],
            }
        filter_specs.append(
            {
                "columnIndex": resolved[f.header],
                "filterCriteria": {"condition": condition},
            }
        )

    sort_specs: list[dict[str, Any]] = [
        {
            "dimensionIndex": resolved[s.header],
            "sortOrder": "DESCENDING" if s.descending else "ASCENDING",
        }
        for s in spec.sorts
    ]

    # Google Sheets API quirk: the addFilterView payload key is ``"filter"``
    # (containing a FilterView resource), NOT ``"filterView"``. The API
    # rejects ``"filterView"`` with: Unknown name "filterView" at
    # 'requests[0].add_filter_view': Cannot find field. Verified against
    # the v4 discovery doc (AddFilterViewRequest schema). If you change
    # this back to ``"filterView"``, the BTD_Candidates saved filter view
    # will fail to create and the receipt will show
    # filter_views_applied: [] — tests in tests/test_sheet_beautifier.py
    # pin the correct key.
    request = {
        "addFilterView": {
            "filter": {
                "title": spec.name,
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(row_count, 2),
                    "startColumnIndex": 0,
                    "endColumnIndex": max(column_count, 1),
                },
                "sortSpecs": sort_specs,
                "filterSpecs": filter_specs,
            }
        }
    }
    return request, []


def _fetch_existing_filter_view_names(
    service,
    spreadsheet_id: str,
    sheet_id: int,
) -> set[str]:
    """Return the set of NamedFilterView titles already attached to ``sheet_id``.

    Used to keep ``apply_filter_views`` idempotent: re-running the beautifier
    must NOT create duplicate views. The call uses a minimal field mask to
    avoid pulling in column data we don't need.
    """
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties.sheetId,filterViews.title)",
        )
        .execute()
    )
    existing: set[str] = set()
    for sheet in response.get("sheets", []):
        if int(sheet.get("properties", {}).get("sheetId", -1)) != sheet_id:
            continue
        for view in sheet.get("filterViews", []) or []:
            title = str(view.get("title", "")).strip()
            if title:
                existing.add(title)
    return existing


@dataclass
class _FilterViewApplyResult:
    """Outcome of one ``apply_filter_views`` call.

    Surfaced via the ``BeautifyReceipt`` so operators can audit per-run which
    views were created vs. skipped (idempotent — view already on the sheet)
    vs. skipped (missing headers — column drift or rename). All three lists
    are kept separate because the same view name is NEVER duplicated in two
    lists: each spec takes exactly one of three outcomes.
    """

    applied: list[_FilterViewSpec] = field(default_factory=list)
    skipped_existing: list[_FilterViewSpec] = field(default_factory=list)
    # Tuple of (view_name, missing_headers) for specs whose column refs don't
    # resolve against the actual sheet headers.
    skipped_missing_headers: list[tuple[str, list[str]]] = field(
        default_factory=list,
    )


def apply_filter_views(
    service,
    spreadsheet_id: str,
    *,
    sheet_name: str,
    sheet_id: int,
    headers: list[str],
    row_count: int,
    specs: list[_FilterViewSpec],
) -> _FilterViewApplyResult:
    """Create the requested NamedFilterViews on ``sheet_name``.

    Idempotent: any spec whose ``name`` already exists on the sheet is skipped
    (no duplicate views). Specs whose required headers are missing in the
    sheet (e.g. column drift / rename) are also skipped + logged.

    Returns a ``_FilterViewApplyResult`` partitioning specs into
    applied / skipped_existing / skipped_missing_headers so the caller can
    populate the receipt without re-inspecting the sheet.
    """
    result = _FilterViewApplyResult()
    if not specs:
        return result

    existing = _fetch_existing_filter_view_names(
        service, spreadsheet_id, sheet_id,
    )
    requests: list[dict[str, Any]] = []
    for spec in specs:
        if spec.name in existing:
            logger.info(
                "Filter view %r already exists on %r — skipping (idempotent).",
                spec.name, sheet_name,
            )
            result.skipped_existing.append(spec)
            continue
        request, missing = _build_filter_view_request(
            spec,
            headers=headers,
            sheet_id=sheet_id,
            row_count=row_count,
        )
        if request is None:
            logger.warning(
                "Filter view %r on %r skipped — missing headers: %s",
                spec.name, sheet_name, ", ".join(missing),
            )
            result.skipped_missing_headers.append((spec.name, list(missing)))
            continue
        requests.append(request)
        result.applied.append(spec)
        logger.info(
            "Filter view %r on %r: %d filter specs, %d sort specs.",
            spec.name, sheet_name,
            len(request["addFilterView"]["filter"]["filterSpecs"]),
            len(request["addFilterView"]["filter"]["sortSpecs"]),
        )

    if not requests:
        return result

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests},
    ).execute()
    return result


# ---------------------------------------------------------------------------
# Number format classification
# ---------------------------------------------------------------------------

# Keywords that suggest currency formatting
_CURRENCY_KEYWORDS = (
    "amount", "capital", "value", "price", "cost", "revenue", "ebitda",
    "ev", "market cap", "purchase", "strike", "transaction value",
    "penalty", "bonus", "salary", "compensation",
)

# Headers that suggest billions-scale currency
_BILLIONS_KEYWORDS = ("ev (b)", "revenue ttm (b)")

# Headers that suggest percentage formatting
_PERCENT_KEYWORDS = (
    "%", "margin", "growth", "ratio", "rate", "coverage",
    "yield", "confidence", "weight", "return",
)

# Headers that suggest date formatting
_DATE_KEYWORDS = (
    "date", " at", " seen", "updated", "created", "observed",
    "filed", "expiry", "decided", "expiration", "maturity",
    "ingested", "notified", "timestamp",
)

# Headers that suggest integer formatting
_INTEGER_KEYWORDS = (
    "count", "number", "days", "buyers",
    "members", "employees", "row", "span", "message id",
    "update id", "telegram message id",
)

# Headers that don't need number formatting (text/boolean)
_TEXT_KEYWORDS = (
    "?", "name", "source", "status", "class", "route",
    "decision", "reason", "notes", "summary", "case",
    "case study", "json", "details", "display", "type",
    "ticker", "symbol", "role", "chamber", "party",
    "state", "text", "description", "comment", "title",
    "security", "nature", "document", "url", "hash",
    "fingerprint", "side", "code", "flag", "owner",
    "identity", "bioguide", "committee", "agency",
    "sector", "industry", "exposure", "error",
    "confirmation", "priority", "stage", "route",
    "candidate", "review", "by", "actor", "field",
    "relationship", "office", "level", "flow",
    "input", "cik", "accession", "id", "key",
    "signal", "trade", "record", "scan",
)


def _header_key(header: str) -> str:
    return header.strip().lower()


def _is_currency(header: str) -> bool:
    key = _header_key(header)
    if any(kw in key for kw in _BILLIONS_KEYWORDS):
        return True
    return any(kw in key for kw in _CURRENCY_KEYWORDS) and not any(
        kw in key for kw in _PERCENT_KEYWORDS
    )


def _is_billions(header: str) -> bool:
    return any(kw in _header_key(header) for kw in _BILLIONS_KEYWORDS)


def _is_percentage(header: str) -> bool:
    key = _header_key(header)
    if "%" in key:
        return True
    return any(kw in key for kw in _PERCENT_KEYWORDS) and not _is_currency(header)


def _is_date(header: str) -> bool:
    return any(kw in _header_key(header) for kw in _DATE_KEYWORDS)


def _is_integer(header: str) -> bool:
    key = _header_key(header)
    if any(kw in key for kw in _INTEGER_KEYWORDS):
        return True
    return False


def _is_text(header: str) -> bool:
    key = _header_key(header)
    return any(kw in key for kw in _TEXT_KEYWORDS)


def classify_headers(headers: list[str]) -> dict[str, list[int]]:
    """Return a dict mapping format types to zero-based column indices."""
    result: dict[str, list[int]] = {
        "currency": [],
        "currency_billions": [],
        "percentage": [],
        "date": [],
        "integer": [],
        "text": [],
    }
    for idx, header in enumerate(headers):
        if _is_text(header):
            result["text"].append(idx)
        elif _is_billions(header):
            result["currency_billions"].append(idx)
        elif _is_currency(header):
            result["currency"].append(idx)
        elif _is_percentage(header):
            result["percentage"].append(idx)
        elif _is_date(header):
            result["date"].append(idx)
        elif _is_integer(header):
            result["integer"].append(idx)
        else:
            result["text"].append(idx)
    return result


# ---------------------------------------------------------------------------
# Sheet metadata helpers
# ---------------------------------------------------------------------------
def _sheet_metadata(service, spreadsheet_id: str) -> dict[str, dict[str, Any]]:
    response = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    meta: dict[str, dict[str, Any]] = {}
    for sheet in response.get("sheets", []):
        props = sheet.get("properties", {})
        title = str(props.get("title", "")).strip()
        if title:
            meta[title] = props
    return meta


# ---------------------------------------------------------------------------
# Formatting operations
# ---------------------------------------------------------------------------
def apply_header_format(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
) -> None:
    """Bold, wrap, and freeze the header row."""
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": max(column_count, 1),
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 10},
                        "wrapStrategy": "WRAP",
                        "backgroundColor": {
                            "red": 0.957,
                            "green": 0.961,
                            "blue": 0.969,
                        },
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": (
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.fontSize,"
                    "userEnteredFormat.wrapStrategy,"
                    "userEnteredFormat.backgroundColor,"
                    "userEnteredFormat.horizontalAlignment"
                ),
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def apply_zebra_stripes(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
    row_count: int,
) -> None:
    """Add alternating row colours from row 2 onwards."""
    if row_count < 2:
        return
    requests = [
        {
            "addBanding": {
                "bandedRange": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": max(row_count, 2),
                        "startColumnIndex": 0,
                        "endColumnIndex": max(column_count, 1),
                    },
                    "rowProperties": {
                        "firstBandColor": ZEBRA_FIRST,
                        "secondBandColor": ZEBRA_SECOND,
                        "headerColor": ZEBRA_SECOND,
                    },
                }
            }
        }
    ]
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
    except Exception as exc:
        logger.warning("Zebra stripes skipped for sheet %d: %s", sheet_id, exc)


def apply_column_widths(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
) -> None:
    """Auto-resize columns to fit content."""
    requests = [
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": max(column_count, 1),
                }
            }
        }
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def apply_number_formats(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    column_count: int,
    header_classes: dict[str, list[int]],
) -> None:
    """Apply per-column number formats based on header classification."""
    format_map = {
        "currency": "$#,##0.00",
        "currency_billions": "$#,##0.00",
        "percentage": "0.00%",
        "date": "yyyy-mm-dd",
        "integer": "#,##0",
        "text": "@",
    }

    requests: list[dict[str, Any]] = []
    for fmt_key, col_indices in header_classes.items():
        pattern = format_map.get(fmt_key, "@")
        if fmt_key == "text":
            continue  # default is already text
        for col_idx in col_indices:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": pattern,
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()


def apply_tab_colour(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    colour: dict[str, float],
) -> None:
    """Set the sheet tab colour."""
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "tabColor": colour,
                },
                "fields": "tabColor",
            }
        }
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


# ---------------------------------------------------------------------------
# Missing sheet columns — reading headers without known schema
# ---------------------------------------------------------------------------
def _read_existing_headers(
    service, spreadsheet_id: str, sheet_name: str, max_cols: int = 100
) -> list[str]:
    end_col = _column_letter(max_cols)
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1:{end_col}1",
            )
            .execute()
        )
        raw = response.get("values", [[]])[0]
        return [str(v or "").strip() for v in raw]
    except Exception:
        return []


def _read_row_count(
    metadata: dict[str, dict[str, Any]], sheet_name: str, fallback: int = 2000
) -> int:
    """Return the effective row count from pre-fetched sheet metadata."""
    try:
        props = metadata.get(sheet_name)
        if props:
            grid = props.get("gridProperties", {})
            return max(int(grid.get("rowCount", fallback)), 2)
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Dry-run receipt
# ---------------------------------------------------------------------------
@dataclass
class BeautifyReceipt:
    sheets_processed: list[str] = field(default_factory=list)
    sheets_skipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False
    # Filter-view telemetry — populated by ``beautify_sheets``. Operators
    # inspect these to confirm their saved views exist (applied), were
    # skipped because they already exist (idempotent), or were skipped
    # because the sheet's columns don't carry what the spec references.
    filter_views_applied: list[str] = field(default_factory=list)
    filter_views_skipped_existing: list[str] = field(default_factory=list)
    filter_views_skipped_missing_headers: list[dict[str, Any]] = field(
        default_factory=list,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def beautify_sheets(
    *,
    dry_run: bool = False,
    sheet_filter: list[str] | None = None,
) -> BeautifyReceipt:
    """Apply all formatting to production sheets."""
    receipt = BeautifyReceipt(dry_run=dry_run)

    service = get_sheets_service(readonly=False)
    spreadsheet_id = get_spreadsheet_id()
    metadata = _sheet_metadata(service, spreadsheet_id)

    filter_set = {s.strip() for s in sheet_filter} if sheet_filter else None

    for sheet_name, known_headers in PRODUCTION_SHEETS:
        if filter_set and sheet_name not in filter_set:
            receipt.sheets_skipped.append(sheet_name)
            continue

        props = metadata.get(sheet_name)
        if props is None:
            logger.info("Sheet %r not found — skipping.", sheet_name)
            receipt.sheets_skipped.append(sheet_name)
            continue

        sheet_id = int(props.get("sheetId", 0))

        # Determine headers
        headers = list(known_headers)
        if not headers:
            headers = _read_existing_headers(service, spreadsheet_id, sheet_name)
        if not headers:
            logger.warning("No headers found for %r — skipping formatting.", sheet_name)
            receipt.sheets_skipped.append(sheet_name)
            continue

        column_count = len(headers)
        row_count = _read_row_count(metadata, sheet_name)
        header_classes = classify_headers(headers)
        tab_colour = TAB_COLOURS.get(sheet_name, {"red": 0.8, "green": 0.8, "blue": 0.8})

        logger.info(
            "Beautifying %r (%d cols × %d rows) …",
            sheet_name,
            column_count,
            row_count,
        )
        logger.info("  Currency: %d  %%: %d  Date: %d  Int: %d  Text: %d",
            len(header_classes["currency"]) + len(header_classes["currency_billions"]),
            len(header_classes["percentage"]),
            len(header_classes["date"]),
            len(header_classes["integer"]),
            len(header_classes["text"]),
        )

        if dry_run:
            # Dry-run still surfaces what filter views WOULD be created so
            # operators see the intended outcome in the receipt + log.
            specs_for_sheet = FILTER_VIEW_CONFIG.get(sheet_name, [])
            if specs_for_sheet:
                receipt.filter_views_applied.extend(
                    s.name for s in specs_for_sheet
                )
                logger.info(
                    "[dry-run] would create %d filter view(s) on %r: %s",
                    len(specs_for_sheet), sheet_name,
                    ", ".join(s.name for s in specs_for_sheet),
                )
            receipt.sheets_processed.append(sheet_name)
            continue

        try:
            apply_header_format(service, spreadsheet_id, sheet_id, column_count)
            apply_zebra_stripes(service, spreadsheet_id, sheet_id, column_count, row_count)
            apply_column_widths(service, spreadsheet_id, sheet_id, column_count)
            apply_number_formats(service, spreadsheet_id, sheet_id, column_count, header_classes)
            apply_tab_colour(service, spreadsheet_id, sheet_id, tab_colour)
            # Saved filter views — only the sheets listed in FILTER_VIEW_CONFIG.
            # ``apply_filter_views`` is idempotent (existing views are skipped),
            # and the per-view outcome is recorded on the receipt so the
            # operator can audit what was created vs. skipped per cycle.
            specs_for_sheet = FILTER_VIEW_CONFIG.get(sheet_name, [])
            if specs_for_sheet:
                fv_result = apply_filter_views(
                    service,
                    spreadsheet_id,
                    sheet_name=sheet_name,
                    sheet_id=sheet_id,
                    headers=headers,
                    row_count=row_count,
                    specs=specs_for_sheet,
                )
                receipt.filter_views_applied.extend(
                    s.name for s in fv_result.applied
                )
                receipt.filter_views_skipped_existing.extend(
                    s.name for s in fv_result.skipped_existing
                )
                receipt.filter_views_skipped_missing_headers.extend(
                    {"view": name, "missing": list(missing)}
                    for name, missing in fv_result.skipped_missing_headers
                )
            receipt.sheets_processed.append(sheet_name)
        except Exception as exc:
            logger.error("Failed to beautify %r: %s", sheet_name, exc)
            receipt.errors.append({"sheet": sheet_name, "error": str(exc)})

    return receipt


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    dry_run = "--dry-run" in sys.argv
    sheet_filter_str = os.getenv("BEAUTIFY_SHEETS", "").strip()
    sheet_filter = (
        [s.strip() for s in sheet_filter_str.split(",") if s.strip()]
        if sheet_filter_str
        else None
    )

    if dry_run:
        print("DRY RUN — no writes will be performed.\n")

    receipt = beautify_sheets(dry_run=dry_run, sheet_filter=sheet_filter)

    print()
    print("SHEET BEAUTIFIER")
    print("=" * 50)
    print(f"Mode:         {'DRY RUN' if dry_run else 'APPLIED'}")
    print(f"Processed:    {len(receipt.sheets_processed)}")
    print(f"Skipped:      {len(receipt.sheets_skipped)}")
    print(f"Errors:       {len(receipt.errors)}")
    if receipt.sheets_processed:
        print(f"Sheets:       {', '.join(receipt.sheets_processed)}")
    if receipt.filter_views_applied:
        print(f"Filter views: {len(receipt.filter_views_applied)} applied")
        for name in receipt.filter_views_applied:
            print(f"  ✓ {name}")
    if receipt.filter_views_skipped_existing:
        print(
            f"  ({len(receipt.filter_views_skipped_existing)} "
            f"skipped — already existed)"
        )
    if receipt.filter_views_skipped_missing_headers:
        print(
            f"  ({len(receipt.filter_views_skipped_missing_headers)} "
            f"skipped — missing headers)"
        )
    if receipt.errors:
        for err in receipt.errors:
            print(f"  ! {err['sheet']}: {err['error']}")

    # Write receipt
    output_dir = Path(os.getenv("FUNNEL_OUTPUT_DIR", "funnel_output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / "sheet_beautifier_receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "dry_run": receipt.dry_run,
                "sheets_processed": receipt.sheets_processed,
                "sheets_skipped": receipt.sheets_skipped,
                "errors": receipt.errors,
                "filter_views_applied": receipt.filter_views_applied,
                "filter_views_skipped_existing": receipt.filter_views_skipped_existing,
                "filter_views_skipped_missing_headers": receipt.filter_views_skipped_missing_headers,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"\nReceipt:      {receipt_path}")
    print("SHEET BEAUTIFIER COMPLETED")


if __name__ == "__main__":
    main()
