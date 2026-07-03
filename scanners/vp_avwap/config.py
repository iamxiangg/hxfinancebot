from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


CALCULATION_VERSION = "2026-07-vp-avwap-v1"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number.") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _env_list(name: str) -> list[str]:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return []
    seen: set[str] = set()
    output: list[str] = []
    for part in raw.split(","):
        ticker = part.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            output.append(ticker)
    return output


@dataclass(frozen=True)
class VpAvwapConfig:
    test_tickers: list[str]
    max_tickers: int | None
    dry_run: bool
    write_sheets: bool
    send_telegram: bool
    telegram_test_mode: bool
    calibration: bool
    rows: int
    value_area_pct: float
    primary_interval: str
    secondary_interval: str
    confluence_pct: float
    zone_buffer_pct: float
    approach_pct: float
    invalidation_buffer_pct: float
    extension_pct: float
    avwap_slope_lookback: int
    avwap_flat_threshold_pct: float
    falling_override_pct: float
    breakout_buffer_pct: float
    breakout_retest_window: int
    output_dir: Path
    daily_period: str = "2y"
    earnings_limit: int = 20
    minimum_profile_bars: int = 3
    auto_adjust: bool = False
    regular_hours_only: bool = True
    calculation_version: str = CALCULATION_VERSION

    @classmethod
    def from_env(cls) -> "VpAvwapConfig":
        config = cls(
            test_tickers=_env_list("VP_AVWAP_TEST_TICKERS"),
            max_tickers=_env_optional_int("VP_AVWAP_MAX_TICKERS"),
            dry_run=_env_bool("VP_AVWAP_DRY_RUN", False),
            write_sheets=_env_bool("VP_AVWAP_WRITE_SHEETS", True),
            send_telegram=_env_bool("VP_AVWAP_SEND_TELEGRAM", False),
            telegram_test_mode=_env_bool("VP_AVWAP_TELEGRAM_TEST_MODE", False),
            calibration=_env_bool("VP_AVWAP_CALIBRATION", False),
            rows=_env_int("VP_AVWAP_ROWS", 60),
            value_area_pct=_env_float("VP_AVWAP_VALUE_AREA_PCT", 70.0),
            primary_interval=str(os.getenv("VP_AVWAP_PRIMARY_INTERVAL", "30m")).strip() or "30m",
            secondary_interval=str(os.getenv("VP_AVWAP_SECONDARY_INTERVAL", "60m")).strip() or "60m",
            confluence_pct=_env_float("VP_AVWAP_CONFLUENCE_PCT", 1.5),
            zone_buffer_pct=_env_float("VP_AVWAP_ZONE_BUFFER_PCT", 0.5),
            approach_pct=_env_float("VP_AVWAP_APPROACH_PCT", 2.0),
            invalidation_buffer_pct=_env_float("VP_AVWAP_INVALIDATION_BUFFER_PCT", 0.5),
            extension_pct=_env_float("VP_AVWAP_EXTENSION_PCT", 8.0),
            avwap_slope_lookback=_env_int("VP_AVWAP_AVWAP_SLOPE_LOOKBACK", 5),
            avwap_flat_threshold_pct=_env_float("VP_AVWAP_AVWAP_FLAT_THRESHOLD_PCT", 0.25),
            falling_override_pct=_env_float("VP_AVWAP_FALLING_OVERRIDE_PCT", -0.50),
            breakout_buffer_pct=_env_float("VP_AVWAP_BREAKOUT_BUFFER_PCT", 0.5),
            breakout_retest_window=_env_int("VP_AVWAP_BREAKOUT_RETEST_WINDOW", 10),
            output_dir=Path(str(os.getenv("VP_AVWAP_OUTPUT_DIR", "funnel_output/vp_avwap")).strip() or "funnel_output/vp_avwap"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.rows < 10:
            raise ValueError("VP_AVWAP_ROWS must be at least 10.")
        if self.value_area_pct <= 0 or self.value_area_pct >= 100:
            raise ValueError("VP_AVWAP_VALUE_AREA_PCT must be between 0 and 100.")
        for name, value in (
            ("VP_AVWAP_CONFLUENCE_PCT", self.confluence_pct),
            ("VP_AVWAP_ZONE_BUFFER_PCT", self.zone_buffer_pct),
            ("VP_AVWAP_APPROACH_PCT", self.approach_pct),
            ("VP_AVWAP_INVALIDATION_BUFFER_PCT", self.invalidation_buffer_pct),
            ("VP_AVWAP_EXTENSION_PCT", self.extension_pct),
            ("VP_AVWAP_BREAKOUT_BUFFER_PCT", self.breakout_buffer_pct),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative.")
        if self.approach_pct < self.zone_buffer_pct:
            raise ValueError("VP_AVWAP_APPROACH_PCT must be at least VP_AVWAP_ZONE_BUFFER_PCT.")
        if self.avwap_slope_lookback < 2:
            raise ValueError("VP_AVWAP_AVWAP_SLOPE_LOOKBACK must be at least 2.")
        if self.breakout_retest_window < 1:
            raise ValueError("VP_AVWAP_BREAKOUT_RETEST_WINDOW must be at least 1.")
        if self.max_tickers is not None and self.max_tickers < 1:
            raise ValueError("VP_AVWAP_MAX_TICKERS must be at least 1 when provided.")
