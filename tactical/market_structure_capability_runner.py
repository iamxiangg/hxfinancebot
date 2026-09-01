from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

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

        split_in_window: bool | None = None
        if "Stock Splits" in working.columns:
            split_values = pd.to_numeric(working["Stock Splits"], errors="coerce").fillna(0.0)
            split_in_window = bool((split_values != 0).any())
            if split_in_window:
                flags["corporate_action_split_in_60d_window"] = True
        else:
            flags["corporate_actions_not_reported_in_download"] = True

        metrics_20d, warnings_20d = _window_metrics(working, 20)
        metrics_60d, warnings_60d = _window_metrics(working, 60)
        warnings = warnings_20d + warnings_60d
        if warnings:
            flags["warnings"] = warnings

        status = "ELIGIBLE"
        # Splits require a separate price/volume normalization policy before production VP use.
        if split_in_window:
            status = "PARTIAL"
        # Timezone is important for global completed-session handling, but absence does not invalidate raw calculations.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Yahoo 60m eligibility for HX 20D/60D fixed VP/VWAP.")
    parser.add_argument("--symbols", type=Path, default=Path("config/market_structure_validation_symbols.txt"))
    parser.add_argument("--output-dir", type=Path, default=Path("funnel_output/market_structure_capability"))
    args = parser.parse_args()

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
    result_path = args.output_dir / "results.json"
    result_path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

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
    summary = {
        "checked_at": checked_at,
        "capability": CAPABILITY,
        "required_interval": INTERVAL,
        "required_sessions": REQUIRED_SESSIONS,
        "symbols_requested": len(symbols),
        "counts": counts,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if counts.get("ERROR", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
