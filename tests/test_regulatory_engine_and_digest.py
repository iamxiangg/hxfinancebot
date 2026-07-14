from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from providers.regulatory.base import SourceBatch
from research.regulatory.engine import RegulatoryMonitorEngine
from research.regulatory.models import RawRegulatoryRecord, SourceTier
from tactical.regulatory_digest import chunk_digest
from tactical.regulatory_runner import _send_telegram


class RegulatoryEngineAndDigestTests(unittest.TestCase):
    def test_provider_failure_does_not_block_successful_source(self) -> None:
        ok_provider = Mock()
        ok_provider.source_name = "ok"
        ok_provider.fetch_changes.return_value = SourceBatch(
            records=[
                RawRegulatoryRecord(
                    raw_event_id="raw-1",
                    source_name="sec",
                    source_record_id="1",
                    source_tier=SourceTier.TIER_1,
                    published_at="2026-07-08",
                    company_name="Apple Inc.",
                    product_name="HX-1",
                    indication_name="Glioblastoma",
                    exact_text="met the primary endpoint",
                )
            ]
        )
        bad_provider = Mock()
        bad_provider.source_name = "bad"
        bad_provider.fetch_changes.return_value = SourceBatch(source_status="ERROR", errors=["boom"])
        engine = RegulatoryMonitorEngine(
            providers={"ok": ok_provider, "bad": bad_provider},
        )
        engine.config.sources = ["ok", "bad"]
        result = engine.run(
            since=datetime(2026, 7, 1, tzinfo=UTC),
            until=datetime(2026, 7, 8, tzinfo=UTC),
        )
        self.assertGreaterEqual(len(result.raw_records), 1)
        self.assertIn("bad", result.provider_errors)
        self.assertEqual(result.provider_errors["bad"], ["boom"])

    def test_unknown_company_mapping_goes_to_unresolved(self) -> None:
        provider = Mock()
        provider.source_name = "sec"
        provider.fetch_changes.return_value = SourceBatch(
            records=[
                RawRegulatoryRecord(
                    raw_event_id="raw-2",
                    source_name="sec",
                    source_record_id="2",
                    source_tier=SourceTier.TIER_1,
                    published_at="2026-07-08",
                    company_name="Unknown Sponsor LLC",
                    product_name="HX-2",
                    indication_name="Rare Disease",
                    exact_text="met the primary endpoint",
                )
            ]
        )
        engine = RegulatoryMonitorEngine(providers={"sec": provider})
        engine.config.sources = ["sec"]
        result = engine.run(
            since=datetime(2026, 7, 1, tzinfo=UTC),
            until=datetime(2026, 7, 8, tzinfo=UTC),
        )
        self.assertEqual(len(result.unresolved), 1)

    def test_regulatory_modules_do_not_reference_feroldi_ai(self) -> None:
        root = Path(__file__).resolve().parents[1]
        targets = [
            root / "research" / "regulatory" / "engine.py",
            root / "research" / "regulatory" / "normalizer.py",
            root / "tactical" / "regulatory_runner.py",
        ]
        for path in targets:
            lowered = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("from funnel.feroldi_ai", lowered)
            self.assertNotIn("import funnel.feroldi_ai", lowered)

    def test_digest_chunking_stays_under_limit(self) -> None:
        text = "\n\n".join(f"SECTION {i}\n" + ("x" * 900) for i in range(5))
        chunks = chunk_digest(text, limit=1500)
        self.assertTrue(all(len(chunk) <= 1515 for chunk in chunks))

    def test_partial_telegram_delivery_reports_partial(self) -> None:
        responses = [Mock(), Exception("boom")]
        responses[0].raise_for_status = Mock()

        def _post(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch("tactical.regulatory_runner.requests.post", side_effect=_post):
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}):
                status, sent, total = _send_telegram("x" * 5000)
        self.assertEqual(status, "PARTIAL")
        self.assertEqual(sent, 1)
        self.assertGreater(total, 1)

    def test_context_only_fda_updates_do_not_enter_digest_rows(self) -> None:
        provider = Mock()
        provider.source_name = "drugs_at_fda"
        provider.fetch_changes.return_value = SourceBatch(
            records=[
                RawRegulatoryRecord(
                    raw_event_id="raw-fda-1",
                    source_name="drugs_at_fda",
                    source_record_id="NDA-1",
                    source_tier=SourceTier.TIER_1,
                    published_at="2026-07-08",
                    company_name="BAXTER HLTHCARE CORP",
                    product_name="LEVOCARNITINE",
                    structured_data={"submission_status": "AP", "status_text": "AP", "status_date": "20260708"},
                )
            ]
        )
        engine = RegulatoryMonitorEngine(providers={"drugs_at_fda": provider})
        engine.config.sources = ["drugs_at_fda"]
        result = engine.run(
            since=datetime(2026, 7, 1, tzinfo=UTC),
            until=datetime(2026, 7, 8, tzinfo=UTC),
        )
        self.assertIsNotNone(result.digest_plan)
        self.assertEqual(len(result.digest_plan.material_events), 0)
        self.assertEqual(len(result.digest_plan.state_updates), 0)
        self.assertEqual(result.digest_plan.other_activity_count, 1)

    def test_historical_sec_precedent_does_not_create_material_digest_alert(self) -> None:
        provider = Mock()
        provider.source_name = "sec"
        provider.fetch_changes.return_value = SourceBatch(
            records=[
                RawRegulatoryRecord(
                    raw_event_id="raw-sec-1",
                    source_name="sec",
                    source_record_id="1",
                    source_tier=SourceTier.TIER_1,
                    published_at="2026-07-14",
                    company_name="Zura Bio Ltd",
                    exact_text=(
                        "Tibulizumab (ZB-106) is an investigational agent. Its efficacy and safety have not been established or approved by the FDA. "
                        "Brodalumab demonstrated improvement in systemic sclerosis outcomes while belimumab showed directionally favorable findings."
                    ),
                )
            ]
        )
        engine = RegulatoryMonitorEngine(providers={"sec": provider})
        engine.config.sources = ["sec"]
        result = engine.run(
            since=datetime(2026, 7, 1, tzinfo=UTC),
            until=datetime(2026, 7, 14, tzinfo=UTC),
        )
        self.assertEqual(len(result.normalized_events), 1)
        event = result.normalized_events[0]
        self.assertEqual(event.normalized_event_type, "HISTORICAL_CLINICAL_PRECEDENT")
        self.assertEqual(result.programmes[event.programme_key].product_name, "Tibulizumab (ZB-106)")
        self.assertEqual(
            result.programmes[event.programme_key].indication_name,
            "Systemic sclerosis / diffuse cutaneous systemic sclerosis",
        )
        self.assertEqual(len(result.digest_plan.material_events), 0)
        self.assertEqual(len(result.digest_plan.state_updates), 0)
        self.assertEqual(result.digest_plan.other_activity_count, 1)


if __name__ == "__main__":
    unittest.main()
