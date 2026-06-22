# NEW — Funnel Pilot shared Google API client

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

CREDENTIALS_ENV = "GCP_SERVICE_ACCOUNT_FILE"
SPREADSHEET_ID_ENV = "GOOGLE_SHEET_ID"

READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

READ_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def load_service_account_info() -> dict[str, Any]:
    """Load service-account JSON from an environment value or local file path."""
    raw_value = os.getenv(CREDENTIALS_ENV, "").strip()
    if not raw_value:
        raise RuntimeError(f"Missing environment variable: {CREDENTIALS_ENV}")

    if raw_value.startswith("{"):
        try:
            info = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{CREDENTIALS_ENV} contains invalid JSON") from exc
    else:
        path = Path(raw_value).expanduser()
        if not path.is_file():
            raise RuntimeError(
                f"{CREDENTIALS_ENV} is neither service-account JSON nor an existing file: {path}"
            )
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read service-account file: {path}") from exc

    required = {"type", "project_id", "private_key", "client_email", "token_uri"}
    missing = required.difference(info)
    if missing:
        raise RuntimeError(
            "Service-account credentials are missing: " + ", ".join(sorted(missing))
        )
    return info


def get_spreadsheet_id() -> str:
    """Return the spreadsheet ID stored in GOOGLE_SHEET_ID."""
    spreadsheet_id = os.getenv(SPREADSHEET_ID_ENV, "").strip()
    if not spreadsheet_id:
        raise RuntimeError(f"Missing environment variable: {SPREADSHEET_ID_ENV}")
    return spreadsheet_id


def get_sheets_service(*, readonly: bool = True):
    """Build an authenticated Google Sheets API service."""
    scopes = READ_ONLY_SCOPES if readonly else READ_WRITE_SCOPES
    credentials = Credentials.from_service_account_info(
        load_service_account_info(),
        scopes=scopes,
    )
    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )
