from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from providers.hx_market_bridge import get_capability_work, ingest_capability_audit
from providers.yahoo_throttle import yahoo_download
from scanners.vp_avwap.avwap import compute_anchored_vwap
from scanners.vp_avwap.profile import build_volume_profile


CAPABILITY = "FIXED_VP_VWAP_20D_60D"
INTERVAL = "60m"
REQUIRED_SESSIONS = 60
WINDOWS = (20, 60)
PROFILE_ROWS = 60
VALUE_AREA_PCT = 70.0


@dataclass
class CapabilityResult:
    provider_symbol: str
    capability: str
    status: str
    checked_at: str
    required_interval: str
    required_sessions: int
    observed_sessions: int
    observed_bars: int
    first_bar_at: str | None
    last_bar_at: str | None
    exchange_timezone: str | None
    split_in_window: bool | None
    quality_flags: dict[str, object]
    last_error: str | None
    metrics_20d: dict[str, float | int | str | None]
    metrics_60d: dict[str, float | int | str | None]


def _load_symbols(path: Path) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        symbol = raw.strip()
        if not symbol or symbol.startswith("#") or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _clean_history(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    working = frame.dropna(how="all").copy()
    if working.empty:
        return working
    if isinstance(working.columns, pd.MultiIndex):
        working.columns = working.columns.get_level_values(0)
    working = working[~working.index.duplicated(keep="last")].sort_index()
    return working


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return str(value)


def _session_dates(frame: pd.DataFrame) -> list[object]:
    return list(dict.fromkeys(pd.Timestamp(idx).date() for idx in frame.index))


def _slice_last_sessions(frame: pd.DataFrame, sessions: int) -> pd.DataFrame:
    dates = _session_dates(frame)
    selected = set(dates[-sessions:])
    return frame[[pd.Timestamp(idx).date() in selected for idx in frame.index]].copy()


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) > 0


def _split_adjustment_audit(frame: pd.DataFrame) -> tuple[bool | None, list[dict[str, object]], bool]:
    """Verify that split events inside the window are reflected in the price series.

    Yahoo/yfinance history is normally split-adjusted even when auto_adjust=False. The
    capability gate nevertheless fails closed when the event-date price gap is materially
    closer to the raw, unadjusted split discontinuity than to ordinary price continuity,
    or when adjacent sessions are unavailable for verification.
    """
    if "Stock Splits" not in frame.columns:
        return None, [], False

    split_values = pd.to_numeric(frame["Stock Splits"], errors="coerce").fillna(0.0)
    event_rows = frame.loc[split_values != 0]
    if event_rows.empty:
        return False, [], True

    session_dates = _session_dates(frame)
    checks: list[dict[str, object]] = []
    all_verified = True
    separation_margin = math.log(1.35)

    for event_index, event_row in event_rows.iterrows():
        ratio = float(event_row["Stock Splits"])
        event_date = pd.Timestamp(event_index).date()
        prior_dates = [date for date in session_dates if date < event_date]
        post_dates = [date for date in session_dates if date >= event_date]

        state = "UNVERIFIED"
        observed_gap_factor: float | None = None
        expected_unadjusted_gap_factor: float | None = None

        if ratio > 0 and prior_dates and post_dates:
            previous_date = prior_dates[-1]
            post_date = post_dates[0]
            previous_session = frame[[pd.Timestamp(index).date() == previous_date for index in frame.index]]
            post_session = frame[[pd.Timestamp(index).date() == post_date for index in frame.index]]
            previous_closes = pd.to_numeric(previous_session["Close"], errors="coerce").dropna()
            post_opens = pd.to_numeric(post_session["Open"], errors="coerce").dropna()

            if not previous_closes.empty and not post_opens.empty:
                previous_close = float(previous_closes.iloc[-1])
                post_open = float(post_opens.iloc[0])
                if _finite_positive(previous_close) and _finite_positive(post_open):
                    observed_gap_factor = post_open / previous_close
                    expected_unadjusted_gap_factor = 1.0 / ratio
                    distance_to_adjusted = abs(math.log(observed_gap_factor))
                    distance_to_unadjusted = abs(
                        math.log(observed_gap_factor / expected_unadjusted_gap_factor)
                    )
                    if distance_to_adjusted + separation_margin < distance_to_unadjusted:
                        state = "ADJUSTED"
                    elif distance_to_unadjusted + separation_margin < distance_to_adjusted:
                        state = "UNADJUSTED"

        if state != "ADJUSTED":
            all_verified = False

        checks.append(
            {
                "event_at": _timestamp(event_index),
                "split_ratio": ratio,
                "observed_gap_factor": observed_gap_factor,
                "expected_unadjusted_gap_factor": expected_unadjusted_gap_factor,
                "state": state,
            }
        )

    return True, checks, all_verified


def _window_metrics(frame: pd.DataFrame, sessions: int) -> tuple[dict[str, float | int | str | None], list[str]]:
    warnings: list[str] = []
    window = _slice_last_sessions(frame, sessions)
    actual_sessions = len(_session_dates(window))
    if actual_sessions < sessions:
        raise ValueError(f"Only {actual_sessions} completed sessions available for {sessions}D window")

    avwap = compute_anchored_vwap(window, slope_lookback_sessions=5)
    if avwap.status != "OK" or not _finite_positive(avwap.current_avwap):
        raise ValueError(avwap.reason or f"{sessions}D VWAP unavailable")

    profile = build_volume_profile(
        window,
        rows=PROFILE_ROWS,
        value_area_pct=VALUE_AREA_PCT,
        current_avwap=avwap.current_avwap,
        interval_used=INTERVAL,
        data_quality="AUDIT",
    )
    if profile.status != "OK":
        raise ValueError(profile.reason or f"{sessions}D volume profile unavailable")

    required_levels = {
        "vwap": avwap.current_avwap,
        "poc": profile.poc,
        "vah": profile.vah,
        "val": profile.val,
        "profile_high": profile.profile_high,
        "profile_low": profile.profile_low,
    }
    for name, value in required_levels.items():
        if value is None or not math.isfinite(float(value)):
            raise ValueError(f"{sessions}D {name} is non-finite")

    if float(profile.val) > float(profile.vah):
        raise ValueError(f"{sessions}D VAL exceeds VAH")
    if not math.isclose(profile.total_source_volume, profile.total_allocated_volume, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(f"{sessions}D profile volume does not reconcile")

    zero_volume_bars = int((pd.to_numeric(window["Volume"], errors="coerce") <= 0).sum())
    if zero_volume_bars:
        warnings.append(f"{sessions}D contains {zero_volume_bars} zero-volume bars")

    return {
        "sessions": actual_sessions,
        "bars": len(window),
        "first_bar_at": _timestamp(window.index[0]),
        "last_bar_at": _timestamp(window.index[-1]),
        "vwap": float(avwap.current_avwap),
        "poc": float(profile.poc),
        "vah": float(profile.vah),
        "val": float(profile.val),
        "profile_high": float(profile.profile_high),
        "profile_low": float(profile.profile_low),
        "total_volume": float(profile.total_source_volume),
    }, warnings


def audit_symbol(symbol: str, *, checked_at: str) -> CapabilityResult:
    flags: dict[str, object] = {}
    try:
        raw = yahoo_download(
            symbol,
            period="6mo",
            interval=INTERVAL,
            auto_adjust=False,
            actions=True,
            repair=False,
            progress=False,
            threads=False,
            prepost=False,
            timeout=15,
            _yahoo_retries=3,
        )
        frame = _clean_history(raw)
        if frame.empty:
            raise ValueError("Yahoo returned no 60m history")

        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ValueError(f"Missing OHLCV columns: {', '.join(missing)}")

        numeric = frame[list(required)].apply(pd.to_numeric, errors="coerce")
        if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy(dtype=float)).all():
            raise ValueError("Malformed or non-finite OHLCV values")
        if (numeric["Volume"] < 0).any():
            raise ValueError("Negative volume present")
        if float(numeric["Volume"].sum()) <= 0:
            raise ValueError("No usable trading volume")

        sessions = _session_dates(frame)
        if len(sessions) < REQUIRED_SESSIONS:
            raise ValueError(f"Only {len(sessions)} distinct 60m sessions returned; need {REQUIRED_SESSIONS}")

        working = _slice_last_sessions(frame, REQUIRED_SESSIONS)
        tz = str(pd.DatetimeIndex(working.index).tz) if pd.DatetimeIndex(working.index).tz is not None else None
        if tz is None:
            flags["timezone_missing"] = True

        split_in_window, split_checks, split_adjustment_verified = _split_adjustment_audit(working)
        if split_in_window is None:
            flags["corporate_actions_not_reported_in_download"] = True
        elif split_in_window:
            flags["corporate_action_split_in_60d_window"] = True
            flags["split_adjustment_checks"] = split_checks
            flags["split_adjustment_verified"] = split_adjustment_verified
            flags["split_adjustment_basis"] = (
                "YAHOO_PROVIDER_SPLIT_ADJUSTED_HISTORY_WITH_PRICE_CONTINUITY_CHECK"
            )

        metrics_20d, warnings_20d = _window_metrics(working, 20)
        metrics_60d, warnings_60d = _window_metrics(working, 60)
        warnings = warnings_20d + warnings_60d
        if warnings:
            flags["warnings"] = warnings

        status = "ELIGIBLE"
        if split_in_window and not split_adjustment_verified:
            status = "PARTIAL"
        if tz is None and status == "ELIGIBLE":
            status = "PARTIAL"

        return CapabilityResult(
            provider_symbol=symbol,
            capability=CAPABILITY,
            status=status,
            checked_at=checked_at,
            required_interval=INTERVAL,
            required_sessions=REQUIRED_SESSIONS,
            observed_sessions=len(_session_dates(working)),
            observed_bars=len(working),
            first_bar_at=_timestamp(working.index[0]),
            last_bar_at=_timestamp(working.index[-1]),
            exchange_timezone=tz,
            split_in_window=split_in_window,
            quality_flags=flags,
            last_error=None,
            metrics_20d=metrics_20d,
            metrics_60d=metrics_60d,
        )
    except ValueError as exc:
        return CapabilityResult(
            provider_symbol=symbol,
            capability=CAPABILITY,
            status="INELIGIBLE",
            checked_at=checked_at,
            required_interval=INTERVAL,
            required_sessions=REQUIRED_SESSIONS,
            observed_sessions=0,
            observed_bars=0,
            first_bar_at=None,
            last_bar_at=None,
            exchange_timezone=None,
            split_in_window=None,
            quality_flags=flags,
            last_error=str(exc),
            metrics_20d={},
            metrics_60d={},
        )
    except Exception as exc:
        return CapabilityResult(
            provider_symbol=symbol,
            capability=CAPABILITY,
            status="ERROR",
            checked_at=checked_at,
            required_interval=INTERVAL,
            required_sessions=REQUIRED_SESSIONS,
            observed_sessions=0,
            observed_bars=0,
            first_bar_at=None,
            last_bar_at=None,
            exchange_timezone=None,
            split_in_window=None,
            quality_flags=flags,
            last_error=f"{exc.__class__.__name__}: {exc}",
            metrics_20d={},
            metrics_60d={},
        )


def _source_reference() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    sha = os.environ.get("GITHUB_SHA", "local")
    return f"GitHub Actions run {run_id} attempt {attempt}; commit {sha}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Yahoo 60m eligibility for HX 20D/60D fixed VP/VWAP.")
    parser.add_argument("--symbols", type=Path, default=Path("config/market_structure_validation_symbols.txt"))
    parser.add_argument("--output-dir", type=Path, default=Path("funnel_output/market_structure_capability"))
    parser.add_argument("--claim-work", action="store_true", help="Read pending capability work from the signed HX market bridge.")
    parser.add_argument("--work-limit", type=int, default=25)
    parser.add_argument("--ingest", action="store_true", help="Persist audit results through the signed HX market bridge.")
    args = parser.parse_args()

    work_items: list[dict[str, object]] = []
    if args.claim_work:
        work_items = get_capability_work(limit=args.work_limit)
        symbols = list(dict.fromkeys(str(item["provider_symbol"]) for item in work_items if item.get("provider_symbol")))
    else:
        symbols = _load_symbols(args.symbols)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    checked_at = datetime.now(UTC).isoformat()

    results: list[CapabilityResult] = []
    for index, symbol in enumerate(symbols, start=1):
        print(f"[{index}/{len(symbols)}] audit {symbol}", flush=True)
        result = audit_symbol(symbol, checked_at=checked_at)
        print(f"  -> {result.status}{': ' + result.last_error if result.last_error else ''}", flush=True)
        results.append(result)

    payload = [asdict(result) for result in results]
    (args.output_dir / "results.json").write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    csv_path = args.output_dir / "results.csv"
    fields = [
        "provider_symbol", "status", "checked_at", "required_interval", "required_sessions",
        "observed_sessions", "observed_bars", "first_bar_at", "last_bar_at", "exchange_timezone",
        "split_in_window", "last_error",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            writer.writerow({field: row.get(field) for field in fields})

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary: dict[str, object] = {
        "checked_at": checked_at,
        "capability": CAPABILITY,
        "required_interval": INTERVAL,
        "required_sessions": REQUIRED_SESSIONS,
        "symbols_requested": len(symbols),
        "work_items_received": len(work_items),
        "counts": counts,
    }

    if args.ingest and payload:
        summary["ingest_result"] = ingest_capability_audit(payload, source_reference=_source_reference())
    elif args.ingest:
        summary["ingest_result"] = {"status": "NO_WORK"}

    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
    return 0 if counts.get("ERROR", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
