from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scanners.vp_avwap.models import VpAvwapScanResult
from tactical.vp_avwap_runner import main
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
