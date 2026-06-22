# NEW — Funnel Pilot Step 4: Congress scanner adapter

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import congress_bot

from funnel.signal_schema import Signal

logger = logging.getLogger(__name__)
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


def _finite_number(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _classification(result: dict) -> str:
    category = str(result.get("category") or "other").strip().lower()
    return "near_miss" if category == "other" else category


def _remaining_validity_days(result: dict) -> int:
    purchase_days = int(getattr(congress_bot, "PURCHASE_DAYS", 45))
    weighted_age = _finite_number(result.get("weighted_age"), 0.0)
    return max(1, int(math.ceil(purchase_days - weighted_age)))


def result_to_signal(result: dict, observed_at: datetime) -> Signal:
    """Convert one congress_bot result dictionary into the common schema."""
    valid_until = observed_at + timedelta(
        days=_remaining_validity_days(result)
    )
    details = {
        "model_version": getattr(congress_bot, "MODEL_VERSION", "unknown"),
        "conviction": round(_finite_number(result.get("conviction")), 2),
        "entry_quality": round(_finite_number(result.get("entry")), 2),
        "bullish_capital_low": _finite_number(result.get("low")),
        "bullish_capital_mid": _finite_number(result.get("mid")),
        "bullish_capital_high": _finite_number(result.get("high")),
        "effective_bullish_capital": _finite_number(result.get("effective")),
        "buyers": int(_finite_number(result.get("buyers"))),
        "cluster_buyers": int(_finite_number(result.get("cluster_buyers"))),
        "weighted_age_days": round(
            _finite_number(result.get("weighted_age")), 2
        ),
        "return_since_activity_pct": round(
            _finite_number(result.get("weighted_return")), 2
        ),
        "flow": str(result.get("flow") or "").strip(),
        "names": list(result.get("names") or []),
        "original_category": str(result.get("category") or "other"),
    }
    return Signal(
        ticker=result.get("ticker"),
        scanner="congress",
        classification=_classification(result),
        score=_finite_number(result.get("conviction")),
        observed_at=observed_at.isoformat(),
        valid_until=valid_until.isoformat(),
        details=details,
    )


def get_congress_signals(min_conviction: float = 15.0) -> list[Signal]:
    """
    Run congress_bot's existing data and scoring functions without Telegram.

    Actionable, wait and risk results are retained. Other results are retained
    only when conviction meets min_conviction, matching the monitor's near-miss
    concept.
    """
    observed_at = datetime.now(SINGAPORE_TZ)
    congress_bot.init_yf()
    trades = congress_bot.fetch_trades()
    if trades is None:
        raise RuntimeError("Congress trade feed could not be retrieved")

    results = congress_bot.process(trades)
    signals: list[Signal] = []
    for result in results:
        category = str(result.get("category") or "other").lower()
        conviction = _finite_number(result.get("conviction"))
        if category == "other" and conviction < float(min_conviction):
            continue
        signals.append(result_to_signal(result, observed_at))

    signals.sort(
        key=lambda signal: (
            signal.classification == "actionable",
            signal.classification == "wait",
            signal.score or 0.0,
            signal.ticker,
        ),
        reverse=True,
    )
    logger.info("Created %d Congress pilot signals", len(signals))
    return signals
