from __future__ import annotations

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


def append_records(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    records: list[dict[str, Any]],
) -> None:
    if not records:
        return

    values = [
        [record.get(header, "") for header in headers]
        for record in records
    ]
    service.spreadsheets().values().append(
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
