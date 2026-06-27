from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


NY_TZ = ZoneInfo("America/New_York")
DEFAULT_STATE_PATH = Path("earnings_notification_state.json")


def state_path() -> Path:
    return Path(os.getenv("EARNINGS_STATE_PATH", str(DEFAULT_STATE_PATH)))


def load_state(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or state_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load earnings state: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Unable to load earnings state: invalid shape")
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def save_state(state: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(target)


def cleanup_state(
    state: dict[str, dict[str, Any]],
    *,
    now_ny: datetime,
    retention_days: int,
) -> dict[str, dict[str, Any]]:
    cutoff = now_ny - timedelta(days=retention_days)
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in state.items():
        earnings_at = str(value.get("earnings_at") or "").strip()
        try:
            stamp = datetime.fromisoformat(earnings_at.replace("Z", "+00:00")).astimezone(NY_TZ)
        except ValueError:
            continue
        if stamp >= cutoff:
            cleaned[key] = dict(value)
    return cleaned


def notification_key(ticker: str, event_date_key: str, earnings_timing: str) -> str:
    return f"{ticker.upper()}|{event_date_key}|{earnings_timing.upper()}"


def should_send_pre_event(state: dict[str, dict[str, Any]], key: str) -> bool:
    return key not in state or not str(state[key].get("pre_event_notified_at") or "").strip()


def should_send_exit(state: dict[str, dict[str, Any]], key: str) -> bool:
    if key not in state:
        return False
    return not str(state[key].get("exit_notified_at") or "").strip()


def record_pre_event_notification(
    state: dict[str, dict[str, Any]],
    *,
    key: str,
    classification: str,
    notified_at: datetime,
    earnings_at: datetime,
    option_expiry: str,
    short_strike: float,
    long_put_strike: float,
    long_call_strike: float,
    entry_estimated_credit: float,
    entry_spot_price: float,
    pre_event_implied_move_pct: float | None,
) -> None:
    state[key] = {
        "classification": classification,
        "pre_event_notified_at": notified_at.isoformat(),
        "exit_notified_at": None,
        "earnings_at": earnings_at.isoformat(),
        "option_expiry": option_expiry,
        "short_strike": short_strike,
        "long_put_strike": long_put_strike,
        "long_call_strike": long_call_strike,
        "entry_estimated_credit": entry_estimated_credit,
        "entry_spot_price": entry_spot_price,
        "pre_event_implied_move_pct": pre_event_implied_move_pct,
    }


def mark_exit_notified(state: dict[str, dict[str, Any]], *, key: str, notified_at: datetime) -> None:
    if key not in state:
        return
    state[key]["exit_notified_at"] = notified_at.isoformat()
