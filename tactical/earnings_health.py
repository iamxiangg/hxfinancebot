"""Workstream E4: Persist earnings scan health receipt."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scanners.earnings.models import ScanHealth


NY_TZ = ZoneInfo("America/New_York")
DEFAULT_OUTPUT_DIR = Path("funnel_output")


def _output_dir() -> Path:
    return Path(os.getenv("FUNNEL_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))


def write_health_receipt(
    *,
    health: ScanHealth,
    mode: str,
    candidate_count: int,
    delivery_attempted: int = 0,
    delivery_succeeded: int = 0,
    delivery_failed: int = 0,
    now_ny: datetime | None = None,
) -> Path:
    output = _output_dir()
    output.mkdir(parents=True, exist_ok=True)
    now_ny = now_ny or datetime.now(NY_TZ)
    commit_sha = os.getenv("GITHUB_SHA", "unknown")

    receipt = {
        "run_timestamp": now_ny.isoformat(),
        "git_commit_sha": commit_sha,
        "scanner_mode": mode,
        "universe_source": health.universe_source,
        "universe_size": health.universe_size,
        "history_attempts": health.history_attempts,
        "history_ok": health.history_ok,
        "history_failures": health.history_failures,
        "earnings_attempts": health.earnings_attempts,
        "confirmed_upcoming": health.confirmed_upcoming,
        "option_expiry_success": health.option_expiry_success,
        "option_chain_success": health.option_chain_success,
        "health_status": health.status,
        "health_reasons": health.health_reasons,
        "provider_failure_categories": health.provider_failure_categories,
        "candidate_count": candidate_count,
        "delivery_attempted": delivery_attempted,
        "delivery_succeeded": delivery_succeeded,
        "delivery_failed": delivery_failed,
    }

    path = output / "earnings_health_receipt.json"
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path
