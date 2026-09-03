from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from providers.hx_market_bridge import (
    get_capability_work,
    ingest_capability_audit,
    ingest_market_structure_snapshot,
)
from tactical.market_structure_capability_runner import INTERVAL, audit_symbol


CALCULATION_VERSION = "HX_MARKET_STRUCTURE_FIXED_v1"
WINDOWS = (20, 60)


def _github_context() -> tuple[str, str, str]:
    return (
        os.environ.get("GITHUB_RUN_ID", "local"),
        os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        os.environ.get("GITHUB_SHA", "local"),
    )


def _source_reference() -> str:
    run_id, attempt, sha = _github_context()
    return f"GitHub Actions run {run_id} attempt {attempt}; commit {sha}; L5 targeted market structure refresh"


def _snapshot_item(result: Any, *, sessions: int) -> dict[str, Any]:
    metrics = result.metrics_20d if sessions == 20 else result.metrics_60d
    warnings = result.quality_flags.get("warnings", []) if isinstance(result.quality_flags, dict) else []
    return {
        "provider_symbol": result.provider_symbol,
        "window_sessions": sessions,
        "vwap": metrics["vwap"],
        "poc": metrics["poc"],
        "vah": metrics["vah"],
        "val": metrics["val"],
        "profile_high": metrics["profile_high"],
        "profile_low": metrics["profile_low"],
        "source_interval": INTERVAL,
        "source_session_count": metrics["sessions"],
        "source_bar_count": metrics["bars"],
        "first_bar_at": metrics.get("first_bar_at"),
        "last_bar_at": metrics.get("last_bar_at"),
        "exchange_timezone": result.exchange_timezone,
        "currency": None,
        "data_quality": "VALIDATED_WITH_WARNINGS" if warnings else "VALIDATED",
        "quality_flags": result.quality_flags,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process queued L5 market-structure work and persist targeted 20D/60D VP/VWAP snapshots."
    )
    parser.add_argument("--work-limit", type=int, default=50)
    parser.add_argument("--output-dir", type=Path, default=Path("funnel_output/l5_market_structure_refresh"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_items = get_capability_work(limit=max(1, min(50, args.work_limit)))
    symbols = list(dict.fromkeys(str(item["provider_symbol"]) for item in work_items if item.get("provider_symbol")))
    checked_at = datetime.now(UTC).isoformat()

    if not symbols:
        summary = {
            "status": "NO_WORK",
            "checked_at": checked_at,
            "work_items_received": len(work_items),
            "symbols_requested": 0,
            "persisted_instruments": 0,
        }
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2), flush=True)
        return 0

    results = []
    for index, symbol in enumerate(symbols, start=1):
        print(f"[{index}/{len(symbols)}] L5 refresh {symbol}", flush=True)
        result = audit_symbol(symbol, checked_at=checked_at)
        print(f"  -> {result.status}{': ' + result.last_error if result.last_error else ''}", flush=True)
        results.append(result)

    capability_payload = [asdict(result) for result in results]
    capability_ingest = ingest_capability_audit(
        capability_payload,
        source_reference=_source_reference(),
    )

    snapshot_items: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []
    for result in results:
        if result.status != "ELIGIBLE":
            exceptions.append(
                {
                    "provider_symbol": result.provider_symbol,
                    "status": result.status,
                    "reason": result.last_error or json.dumps(result.quality_flags, sort_keys=True),
                }
            )
            continue
        for sessions in WINDOWS:
            snapshot_items.append(_snapshot_item(result, sessions=sessions))

    snapshot_ingest: dict[str, Any] = {"status": "NO_ELIGIBLE_SNAPSHOT_ITEMS"}
    if snapshot_items:
        run_id, run_attempt, git_sha = _github_context()
        ingest_key = f"FIXED_VP_VWAP_20D_60D:L5:{run_id}:{run_attempt}"
        if run_id == "local":
            ingest_key = f"FIXED_VP_VWAP_20D_60D:L5:local:{git_sha}:{checked_at}"
        run_payload = {
            "snapshot_at": checked_at,
            "source_cutoff_at": checked_at,
            "calculation_version": CALCULATION_VERSION,
            "git_commit_sha": git_sha,
            "source_run_id": run_id,
            "source_run_attempt": int(run_attempt) if str(run_attempt).isdigit() else 1,
            "ingest_key": ingest_key,
            "status": "COMPLETED" if not exceptions else "COMPLETED_WITH_EXCEPTIONS",
            "requested_instruments": len(symbols),
            "is_test": False,
            "summary": {
                "mode": "L5_DECISION_RELEVANT_TARGETED",
                "work_items_received": len(work_items),
                "requested_symbols": symbols,
                "persisted_instruments": len(snapshot_items) // 2,
                "exceptions": exceptions,
                "source_interval": INTERVAL,
                "windows": list(WINDOWS),
            },
        }
        snapshot_ingest = ingest_market_structure_snapshot(run=run_payload, items=snapshot_items)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    summary = {
        "status": "COMPLETED" if not exceptions else "COMPLETED_WITH_EXCEPTIONS",
        "checked_at": checked_at,
        "work_items_received": len(work_items),
        "symbols_requested": len(symbols),
        "persisted_instruments": len(snapshot_items) // 2,
        "detail_rows": len(snapshot_items),
        "counts": counts,
        "capability_ingest": capability_ingest,
        "snapshot_ingest": snapshot_ingest,
    }
    (args.output_dir / "results.json").write_text(
        json.dumps(capability_payload, indent=2, allow_nan=False), encoding="utf-8"
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
    return 0 if counts.get("ERROR", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
