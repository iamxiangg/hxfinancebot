from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from funnel.regulatory_adapter import persist_digest_delivery, run_regulatory_adapter
from research.regulatory.config import RegulatoryMonitorConfig
from scanners.no_llm_guard import check_production_safeguards, raise_if_feroldi_ai_imported, require_no_llm
from tactical.regulatory_digest import chunk_digest


LOCK_FILE = Path("regulatory_monitor.lock")
LOGGER = logging.getLogger("regulatory_monitor")
LOGGER.setLevel(logging.INFO)
LOGGER.handlers.clear()
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
LOGGER.addHandler(_handler)


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _lock() -> bool:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _unlock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _send_telegram(text: str) -> tuple[str, int, int]:
    token = str(os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        return "SKIPPED", 0, 0
    sent = 0
    chunks = chunk_digest(text)
    for chunk in chunks:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=20,
            )
            response.raise_for_status()
            sent += 1
            time.sleep(1)
        except Exception:
            status = "FAILED" if sent == 0 else "PARTIAL"
            return status, sent, len(chunks)
    return "SENT", sent, len(chunks)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic regulatory lifecycle monitor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--sources", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="")
    parser.add_argument("--ticker", default="")
    parser.add_argument("--rebuild-programme", action="store_true")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--write-preview", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    require_no_llm()
    raise_if_feroldi_ai_imported()
    for warning in check_production_safeguards():
        LOGGER.warning(warning)
    if not _lock():
        LOGGER.error("Another regulatory monitor run appears active.")
        return 1
    try:
        cfg = RegulatoryMonitorConfig.from_env()
        if args.local:
            os.environ["REGULATORY_STATE_BACKEND"] = "local"
            cfg.state_backend = "local"
        if args.sources:
            os.environ["REGULATORY_SOURCES"] = args.sources
            cfg.sources = [item.strip() for item in args.sources.split(",") if item.strip()]
        since = datetime.fromisoformat(args.since) if args.since else None
        until = datetime.fromisoformat(args.until) if args.until else None
        adapter_result = run_regulatory_adapter(
            config=cfg,
            since=since,
            until=until,
            preview_path=str(Path(cfg.audit_dir) / "regulatory_digest_preview.txt"),
        )
        sent = False
        telegram_status = "SKIPPED"
        if adapter_result.digest_text and not args.no_telegram and _bool_env("REGULATORY_SEND_TELEGRAM", True):
            telegram_status, sent_count, total_count = _send_telegram(adapter_result.digest_text)
            sent = telegram_status == "SENT"
            LOGGER.info(
                "Regulatory Telegram delivery status=%s sent=%d total=%d.",
                telegram_status,
                sent_count,
                total_count,
            )
        persist_digest_delivery(
            run_result=adapter_result.run_result,
            preview_path=adapter_result.preview_path,
            telegram_included=sent,
            telegram_status=telegram_status,
        )
        LOGGER.info("Regulatory monitor completed. Preview: %s", adapter_result.preview_path)
        return 0
    finally:
        _unlock()


if __name__ == "__main__":
    raise SystemExit(main())
