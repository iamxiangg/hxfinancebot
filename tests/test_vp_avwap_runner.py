from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scanners.vp_avwap.models import VpAvwapScanResult
from tactical.vp_avwap_runner import _material_telegram_message, main
from tests.test_vp_avwap_sheet_writer import _scan_result


class RunnerTests(unittest.TestCase):
    def test_dry_run_creates_local_artefacts_and_prints_report(self) -> None:
        scan_result = _scan_result()
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "VP_AVWAP_DRY_RUN": "true",
                "VP_AVWAP_WRITE_SHEETS": "false",
                "VP_AVWAP_OUTPUT_DIR": tmpdir,
            },
            clear=False,
        ), patch("tactical.vp_avwap_runner.get_stock_summary_ticker_records", return_value=[{"ticker": "AAA"}]), patch(
            "tactical.vp_avwap_runner.run_vp_avwap_scan",
            return_value=scan_result,
        ), patch(
            "sys.stdout",
            new_callable=io.StringIO,
        ) as stdout:
            code = main()
            summary_exists = (Path(tmpdir) / "latest_summary.json").exists()

        self.assertEqual(code, 0)
        self.assertIn("VP/AVWAP TECHNICAL TIERS", stdout.getvalue())
        self.assertTrue(summary_exists)

    def test_telegram_failure_does_not_fail_run(self) -> None:
        scan_result = _scan_result()
        scan_result.results[0].tier_change = "IMPROVED"
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "VP_AVWAP_DRY_RUN": "false",
                "VP_AVWAP_WRITE_SHEETS": "false",
                "VP_AVWAP_SEND_TELEGRAM": "true",
                "VP_AVWAP_OUTPUT_DIR": tmpdir,
            },
            clear=False,
        ), patch("tactical.vp_avwap_runner.get_stock_summary_ticker_records", return_value=[{"ticker": "AAA"}]), patch(
            "tactical.vp_avwap_runner.run_vp_avwap_scan",
            return_value=scan_result,
        ), patch("tactical.vp_avwap_runner.send_telegram_text", return_value=False):
            code = main()

        self.assertEqual(code, 0)

    def test_telegram_sends_grouped_summary_without_material_change(self) -> None:
        scan_result = _scan_result()
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "VP_AVWAP_DRY_RUN": "false",
                "VP_AVWAP_WRITE_SHEETS": "false",
                "VP_AVWAP_SEND_TELEGRAM": "true",
                "VP_AVWAP_OUTPUT_DIR": tmpdir,
            },
            clear=False,
        ), patch("tactical.vp_avwap_runner.get_stock_summary_ticker_records", return_value=[{"ticker": "AAA"}]), patch(
            "tactical.vp_avwap_runner.run_vp_avwap_scan",
            return_value=scan_result,
        ), patch("tactical.vp_avwap_runner.send_telegram_text", return_value=True) as mock_send:
            code = main()

        self.assertEqual(code, 0)
        mock_send.assert_called_once()
        message = mock_send.call_args.args[0]
        self.assertIn("VP/AVWAP TECHNICAL TIERS", message)
        self.assertIn("AAA | APPROACHING | Hold Above VAH | 88", message)
        self.assertIn("Price $101.00 | Zone $99.00-$100.00 | Stop $98.00", message)

    def test_telegram_message_uses_compact_mobile_friendly_layout(self) -> None:
        scan_result = _scan_result()
        scan_result.results[0].preferred_route.zone_low = 99.0
        scan_result.results[0].preferred_route.zone_high = 99.0
        scan_result.results[0].preferred_route.status = "CONFIRMED"
        message = _material_telegram_message(scan_result)

        self.assertIn("Tier 1: 1 (AAA)", message)
        self.assertIn("Changes", message)
        self.assertIn("AAA | CONFIRMED | Tier 1 | Hold Above VAH", message)
        self.assertIn("AAA | CONFIRMED | Hold Above VAH | 88", message)
        self.assertIn("Zone $99.00", message)
        self.assertIn("Trigger Close above $100.00 after VAH hold", message)
        self.assertIn("Chart https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAA", message)
        self.assertNotIn("AAA - TECHNICAL TIER 1", message)

    def test_telegram_message_uses_configured_tradingview_chart_layout(self) -> None:
        scan_result = _scan_result()
        scan_result.results[0].ticker = "ZETA"
        scan_result.results[0].google_ticker = "NYSE:ZETA"
        with patch.dict(os.environ, {"VP_AVWAP_TRADINGVIEW_CHART_ID": "9OmQpc2c"}, clear=False):
            message = _material_telegram_message(scan_result)

        self.assertIn("Chart https://www.tradingview.com/chart/9OmQpc2c/?symbol=NYSE%3AZETA", message)

    def test_telegram_message_limits_to_high_signal_setups(self) -> None:
        scan_result = _scan_result()
        base = scan_result.results[0]
        extras = []
        statuses = ["CONFIRMED", "TESTING", "APPROACHING", "APPROACHING", "WAITING"]
        tickers = ["BBB", "CCC", "DDD", "EEE", "FFF"]
        for ticker, status in zip(tickers, statuses):
            item = copy.deepcopy(base)
            item.ticker = ticker
            item.google_ticker = f"NASDAQ:{ticker}"
            item.preferred_route.status = status
            item.preferred_route.distance_to_zone_pct = {"CONFIRMED": 0.5, "TESTING": 0.0, "APPROACHING": 1.0, "WAITING": 3.0}[status]
            extras.append(item)
        scan_result.results.extend(extras)

        message = _material_telegram_message(scan_result)

        self.assertIn("Actionable Now", message)
        self.assertIn("CCC | TESTING", message)
        self.assertIn("AAA | APPROACHING", message)
        self.assertIn("DDD | APPROACHING", message)
        self.assertIn("BBB | CONFIRMED", message)
        self.assertNotIn("EEE | APPROACHING", message)
        self.assertNotIn("FFF | WAITING", message)
        self.assertIn("More candidates: 1 additional high-priority setups in sheets/artifacts.", message)

    def test_telegram_message_excludes_stretched_confirmed_setup(self) -> None:
        scan_result = _scan_result()
        scan_result.results[0].ticker = "DUOL"
        scan_result.results[0].google_ticker = "NASDAQ:DUOL"
        scan_result.results[0].preferred_route.status = "CONFIRMED"
        scan_result.results[0].preferred_route.distance_to_zone_pct = 3.5

        message = _material_telegram_message(scan_result)

        self.assertIn("Changes", message)
        self.assertIn("DUOL | CONFIRMED | Tier 1 | Hold Above VAH", message)
        self.assertNotIn("Actionable Now\n\nDUOL | CONFIRMED", message)

    def test_invalid_configuration_returns_non_zero(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VP_AVWAP_ROWS": "5",
            },
            clear=False,
        ):
            code = main()

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
