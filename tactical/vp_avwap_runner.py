from __future__ import annotations

import logging
import os
from pathlib import Path

from funnel.sheet_reader import get_stock_summary_ticker_records
from funnel.vp_avwap_report import detailed_results, format_detailed_entry_map, format_grouped_report, write_local_artifacts
from funnel.vp_avwap_sheet_writer import apply_previous_tiers, read_previous_tiers, write_vp_avwap_sheets
from scanners.vp_avwap.config import VpAvwapConfig
from scanners.vp_avwap.engine import run_vp_avwap_scan
from tactical.earnings_telegram import send_telegram_text


logger = logging.getLogger(__name__)


def _material_telegram_message(scan_result) -> str:
    changed = [
        result
        for result in scan_result.results
        if result.tier_change in {"IMPROVED", "DETERIORATED"} or result.preferred_route.status == "CONFIRMED"
    ]
    lines = [format_grouped_report(scan_result)]
    if changed:
        lines.extend(["", "Material changes", ""])
        for result in changed:
            change_label = result.tier_change or result.preferred_route.status
            lines.append(
                f"{result.ticker} - {change_label} - Tier {result.final_tier} - {result.preferred_route.route_label}"
            )
    tier_one = [result for result in scan_result.results if result.final_tier == 1]
    for result in tier_one:
        lines.extend(["", format_detailed_entry_map(result)])
    return "\n".join(lines).strip()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = VpAvwapConfig.from_env()
    except ValueError as exc:
        logger.error("VP/AVWAP configuration invalid: %s", exc)
        return 1

    try:
        ticker_records = get_stock_summary_ticker_records()
    except Exception as exc:
        logger.error("Unable to load Stock Summary USD tickers: %s", exc)
        return 1

    scan_result = run_vp_avwap_scan(ticker_records, config=config)
    previous_tiers: dict[str, int] = {}
    if not config.dry_run:
        try:
            previous_tiers = read_previous_tiers()
        except Exception as exc:
            logger.warning("Previous technical tiers could not be loaded: %s", exc)
    apply_previous_tiers(scan_result.results, previous_tiers)

    output_paths = write_local_artifacts(scan_result, output_dir=Path(config.output_dir))
    print(format_grouped_report(scan_result))
    print("")
    for result in detailed_results(scan_result):
        print(format_detailed_entry_map(result))
        print("")

    if config.write_sheets and not config.dry_run:
        try:
            write_vp_avwap_sheets(scan_result, dry_run=False)
        except Exception as exc:
            logger.error("VP/AVWAP sheet write failed: %s", exc)
            return 1

    if config.send_telegram and not config.dry_run:
        try:
            logger.info(
                "VP/AVWAP Telegram enabled: test_mode=%s token_present=%s chat_id_present=%s",
                config.telegram_test_mode,
                bool(str(os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()),
                bool(str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()),
            )
            message = _material_telegram_message(scan_result)
            if config.telegram_test_mode:
                message = "\n".join(["VP/AVWAP TELEGRAM TEST MODE", "", message]).strip()
            sent = send_telegram_text(message)
            if not sent:
                logger.error("VP/AVWAP Telegram delivery failed.")
        except Exception as exc:
            logger.error("VP/AVWAP Telegram send failed: %s", exc)
    elif not config.dry_run:
        logger.info("VP/AVWAP Telegram disabled; set VP_AVWAP_SEND_TELEGRAM=true to send notifications.")

    logger.info(
        "VP/AVWAP scan complete: processed=%d output_dir=%s",
        scan_result.processed_tickers,
        output_paths["latest_summary.json"].parent,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
