from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from funnel.regulatory_archive import archive_raw_payload, load_regulatory_archive_state, persist_raw_events, persist_unresolved


class RegulatoryArchiveTests(unittest.TestCase):
    def test_local_fallback_persists_raw_events_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "REGULATORY_STATE_DIR": temp_dir,
                "REGULATORY_AUDIT_DIR": temp_dir,
                "REGULATORY_STATE_BACKEND": "local",
            },
            clear=False,
        ):
            state = load_regulatory_archive_state()
            persist_raw_events(
                state,
                [
                    {
                        "Raw Event ID": "raw-1",
                        "Source Name": "sec",
                        "Source Record ID": "0001",
                    }
                ],
            )
            payload = json.loads((Path(temp_dir) / "events_raw.json").read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["Raw Event ID"], "raw-1")

    def test_raw_payloads_are_archived_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "REGULATORY_STATE_DIR": temp_dir,
                "REGULATORY_AUDIT_DIR": temp_dir,
                "REGULATORY_STATE_BACKEND": "local",
            },
            clear=False,
        ):
            path = archive_raw_payload(
                source_name="sec",
                raw_event_id="raw-123",
                payload_hash="hash-123",
                payload={"headline": "FDA approval"},
            )
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["headline"], "FDA approval")

    def test_unresolved_rows_are_compacted_by_issue_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "REGULATORY_STATE_DIR": temp_dir,
                "REGULATORY_AUDIT_DIR": temp_dir,
                "REGULATORY_STATE_BACKEND": "local",
            },
            clear=False,
        ):
            state = load_regulatory_archive_state()
            persist_unresolved(
                state,
                [
                    {
                        "Unresolved ID": "unr-old-1",
                        "Raw Event ID": "raw-1",
                        "Source Record ID": "NDA-1",
                        "Reason": "Exact company mapping unavailable.",
                        "Source Name": "drugs_at_fda",
                        "Source URL": "https://api.fda.gov/drug/drugsfda.json",
                        "Company Name": "KENVUE BRANDS",
                        "Ticker": "",
                        "Trial NCT ID": "",
                        "Product Name": "ZYRTEC",
                        "Required Action": "MANUAL_REVIEW_REQUIRED",
                        "Conflicting Source": "",
                        "Created At": "2026-07-09T13:43:06Z",
                    },
                    {
                        "Unresolved ID": "unr-old-2",
                        "Raw Event ID": "raw-2",
                        "Source Record ID": "NDA-2",
                        "Reason": "Exact company mapping unavailable.",
                        "Source Name": "drugs_at_fda",
                        "Source URL": "https://api.fda.gov/drug/drugsfda.json",
                        "Company Name": "KENVUE BRANDS",
                        "Ticker": "",
                        "Trial NCT ID": "",
                        "Product Name": "ZYRTEC",
                        "Required Action": "MANUAL_REVIEW_REQUIRED",
                        "Conflicting Source": "",
                        "Created At": "2026-07-09T13:46:44Z",
                    },
                ],
            )
            payload = json.loads((Path(temp_dir) / "unresolved.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["Raw Event ID"], "raw-2")


if __name__ == "__main__":
    unittest.main()
