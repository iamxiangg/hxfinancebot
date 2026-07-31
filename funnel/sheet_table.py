from __future__ import annotations

import re
from typing import Any


def column_letter(column_number: int) -> str:
    if column_number < 1:
        raise ValueError("column_number must be at least 1")

    result = ""
    remaining = column_number
    while remaining:
        remaining, remainder = divmod(remaining - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def values_to_records(
    values: list[list[Any]],
    headers: list[str],
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for raw_row in values[1:]:
        record = {
            header: cell_text(raw_row[index]) if index < len(raw_row) else ""
            for index, header in enumerate(headers)
        }
        if any(record.values()):
            records.append(record)
    return records


def ensure_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    *,
    rows: int = 1000,
) -> None:
    metadata = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in metadata.get("sheets", [])
    }
    if sheet_name not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": max(len(headers), 20),
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()

    ensure_headers(service, spreadsheet_id, sheet_name, headers)


def ensure_sheets(
    service,
    spreadsheet_id: str,
    sheets: list[tuple[str, list[str]]],
    *,
    rows: int = 1000,
) -> None:
    """Create missing sheets and repair headers with a bounded number of API calls."""
    if not sheets:
        return

    metadata = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    existing_titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in metadata.get("sheets", [])
    }
    missing_sheets = [
        (sheet_name, headers)
        for sheet_name, headers in sheets
        if sheet_name not in existing_titles
    ]
    if missing_sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": max(len(headers), 20),
                                },
                            }
                        }
                    }
                    for sheet_name, headers in missing_sheets
                ]
            },
        ).execute()

    header_ranges = [
        f"'{sheet_name}'!A1:{column_letter(len(headers))}1"
        for sheet_name, headers in sheets
    ]
    header_response = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=header_ranges,
    ).execute()
    existing_headers = header_response.get("valueRanges", [])
    header_updates: list[dict[str, Any]] = []
    for (sheet_name, headers), header_range, value_range in zip(sheets, header_ranges, existing_headers):
        current = [cell_text(value) for value in value_range.get("values", [[]])[0]]
        if current[: len(headers)] != headers:
            header_updates.append({"range": header_range, "values": [headers]})

    # A missing valueRange represents a new, completely blank sheet.
    for index in range(len(existing_headers), len(sheets)):
        sheet_name, headers = sheets[index]
        header_updates.append(
            {
                "range": header_ranges[index],
                "values": [headers],
            }
        )

    if header_updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": header_updates,
            },
        ).execute()


def ensure_headers(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> None:
    end_col = column_letter(len(headers))
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:{end_col}1",
        )
        .execute()
    )
    existing = response.get("values", [[]])[0]
    existing = [cell_text(value) for value in existing]

    if existing[: len(headers)] != headers:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:{end_col}1",
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        ).execute()


def read_table(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> list[dict[str, str]]:
    end_col = column_letter(len(headers))
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:{end_col}",
        )
        .execute()
    )
    return values_to_records(response.get("values", []), headers)


def read_table_with_row_numbers(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
) -> list[tuple[int, dict[str, str]]]:
    """Read table records while preserving their sheet row numbers for later writes."""
    end_col = column_letter(len(headers))
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:{end_col}",
        )
        .execute()
    )
    rows: list[tuple[int, dict[str, str]]] = []
    for row_number, raw_row in enumerate(response.get("values", [])[1:], start=2):
        record = {
            header: cell_text(raw_row[index]) if index < len(raw_row) else ""
            for index, header in enumerate(headers)
        }
        if any(record.values()):
            rows.append((row_number, record))
    return rows


def append_records(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not records:
        return None

    values = [
        [record.get(header, "") for header in headers]
        for record in records
    ]
    return service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def replace_records(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    records: list[dict[str, Any]],
) -> None:
    end_col = column_letter(len(headers))
    values = [headers] + [[record.get(header, "") for header in headers] for record in records]
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1:{end_col}",
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def upsert_records(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_header: str,
    records: list[dict[str, Any]],
) -> None:
    if not records:
        return

    existing_values = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:{column_letter(len(headers))}",
        )
        .execute()
        .get("values", [])
    )

    try:
        key_index = headers.index(key_header)
    except ValueError as exc:
        raise ValueError(f"Unknown key header: {key_header}") from exc

    row_by_key: dict[str, int] = {}
    for row_number, raw_row in enumerate(existing_values[1:], start=2):
        key = cell_text(raw_row[key_index]) if key_index < len(raw_row) else ""
        if key:
            row_by_key[key.upper()] = row_number

    to_append: list[dict[str, Any]] = []
    update_data: list[dict[str, Any]] = []
    end_col = column_letter(len(headers))

    for record in records:
        key = cell_text(record.get(key_header)).upper()
        values = [[record.get(header, "") for header in headers]]
        if key and key in row_by_key:
            row_number = row_by_key[key]
            update_data.append(
                {
                    "range": f"'{sheet_name}'!A{row_number}:{end_col}{row_number}",
                    "values": values,
                }
            )
        else:
            to_append.append(record)

    if update_data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": update_data,
            },
        ).execute()

    append_records(service, spreadsheet_id, sheet_name, headers, to_append)


def upsert_records_from_loaded_rows(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_header: str,
    records: list[dict[str, Any]],
    row_by_key: dict[str, int],
) -> None:
    """Upsert records using row locations captured during the current sheet read."""
    if not records:
        return
    if key_header not in headers:
        raise ValueError(f"Unknown key header: {key_header}")

    end_col = column_letter(len(headers))
    update_data: list[dict[str, Any]] = []
    to_append: list[dict[str, Any]] = []
    appended_keys: list[str] = []

    for record in records:
        key = cell_text(record.get(key_header)).upper()
        values = [[record.get(header, "") for header in headers]]
        row_number = row_by_key.get(key)
        if key and row_number is not None:
            update_data.append(
                {
                    "range": f"'{sheet_name}'!A{row_number}:{end_col}{row_number}",
                    "values": values,
                }
            )
        else:
            to_append.append(record)
            appended_keys.append(key)

    if update_data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": update_data,
            },
        ).execute()

    append_response = append_records(service, spreadsheet_id, sheet_name, headers, to_append)
    updated_range = str((append_response or {}).get("updates", {}).get("updatedRange") or "")
    match = re.search(r"![A-Z]+(\d+)(?::[A-Z]+\d+)?$", updated_range)
    if match:
        start_row = int(match.group(1))
        for offset, key in enumerate(appended_keys):
            if key:
                row_by_key[key] = start_row + offset
