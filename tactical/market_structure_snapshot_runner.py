from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from providers.hx_market_bridge import (
    get_eligible_universe,
    ingest_capability_audit,
    ingest_market_structure_snapshot,
)
from tactical.market_structure_capability_runner import (
    INTERVAL,
    audit_symbol,
)


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
    return f"GitHub Actions run {run_id} attempt {attempt}; commit {sha}; {CALCULATION_VERSION}"


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
        description="Create the HX 20D/60D fixed VP/VWAP market-structure snapshot from the Supabase eligible universe."
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional deterministic symbol limit for testing; 0 means all eligible symbols.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate and write local artefacts without ingesting results.")
    parser.add_argument("--output-dir", type=Path, default=Path("funnel_output/market_structure_snapshot"))
    args = parser.parse_args()

    universe = get_eligible_universe()
    if args.limit > 0:
        universe = universe[: args.limit]
    if not universe:
        raise RuntimeError("No ELIGIBLE instruments returned by HX market bridge")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    checked_at = datetime.now(UTC).isoformat()

    capability_results = []
    snapshot_items: list[dict[str, Any]] = []
    exceptions: list[dict[str, str]] = []

    for index, item in enumerate(universe, start=1):
        symbol = str(item["provider_symbol"])
        print(f"[{index}/{len(universe)}] snapshot {symbol}", flush=True)
        result = audit_symbol(symbol, checked_at=checked_at)
        capability_results.append(result)
        if result.status != "ELIGIBLE":
            exceptions.append(
                {
                    "provider_symbol": symbol,
                    "status": result.status,
                    "reason": result.last_error or json.dumps(result.quality_flags, sort_keys=True),
                }
            )
            print(f"  -> excluded: {result.status}", flush=True)
            continue

        for sessions in WINDOWS:
            snapshot_items.append(_snapshot_item(result, sessions=sessions))
        print("  -> 20D + 60D validated", flush=True)

    capability_payload = [asdict(result) for result in capability_results]
    (args.output_dir / "capability_refresh.json").write_text(
        json.dumps(capability_payload, indent=2, allow_nan=False), encoding="utf-8"
    )
    (args.output_dir / "snapshot_items.json").write_text(
        json.dumps(snapshot_items, indent=2, allow_nan=False), encoding="utf-8"
    )

    persisted_instruments = len(snapshot_items) // 2
    status = "COMPLETED" if not exceptions else "COMPLETED_WITH_EXCEPTIONS"
    run_id, run_attempt, git_sha = _github_context()
    ingest_key = f"FIXED_VP_VWAP_20D_60D:{run_id}:{run_attempt}"
    if run_id == "local":
        ingest_key = f"FIXED_VP_VWAP_20D_60D:local:{git_sha}:{checked_at}"

    run_payload: dict[str, Any] = {
        "snapshot_at": checked_at,
        "source_cutoff_at": checked_at,
        "calculation_version": CALCULATION_VERSION,
        "git_commit_sha": git_sha,
        "source_run_id": run_id,
        "source_run_attempt": int(run_attempt) if str(run_attempt).isdigit() else 1,
        "ingest_key": ingest_key,
        "status": status,
        "requested_instruments": len(universe),
        "summary": {
            "eligible_at_start": len(universe),
            "persisted_instruments": persisted_instruments,
            "exceptions": exceptions,
            "source_interval": INTERVAL,
            "windows": list(WINDOWS),
        },
    }

    ingest_summary: dict[str, Any] = {"status": "DRY_RUN"}
    if not args.dry_run:
        capability_ingest = ingest_capability_audit(
            capability_payload,
            source_reference=_source_reference(),
        )
        if not snapshot_items:
            raise RuntimeError("No instruments remained eligible after live validation")
        snapshot_ingest = ingest_market_structure_snapshot(
            run=run_payload,
            items=snapshot_items,
        )
        ingest_summary = {
            "capability_ingest": capability_ingest,
            "snapshot_ingest": snapshot_ingest,
        }

    summary = {
        "checked_at": checked_at,
        "calculation_version": CALCULATION_VERSION,
        "requested_instruments": len(universe),
        "persisted_instruments": persisted_instruments,
        "detail_rows": len(snapshot_items),
        "status": status,
        "exception_count": len(exceptions),
        "ingest": ingest_summary,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
