from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import requests


logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _to_float(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _extract_records(payload: dict[str, Any], *candidates: str) -> list[dict[str, Any]]:
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, list):
            return [record for record in value if isinstance(record, dict)]
    return []


def _parse_date(value: Any) -> date | None:
    text = _clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _latest_forward_records(records: list[dict[str, Any]], *, limit: int = 2) -> list[dict[str, Any]]:
    today = _today_utc()
    scored: list[tuple[date, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for record in records:
        record_date = _parse_date(
            record.get("fiscalDateEnding")
            or record.get("fiscal_date_ending")
            or record.get("reportDate")
            or record.get("report_date")
        )
        if record_date is None:
            undated.append(record)
            continue
        if record_date >= today:
            scored.append((record_date, record))
    scored.sort(key=lambda item: item[0])
    selected = [record for _, record in scored[:limit]]
    if selected:
        return selected
    return undated[:limit] or records[:limit]


def _revision_direction(record: dict[str, Any], prefix: str) -> float | None:
    up = _to_float(
        record.get(f"{prefix}RevisionUp")
        or record.get(f"{prefix}RevisionsUp")
        or record.get(f"{prefix}_revision_up")
    )
    down = _to_float(
        record.get(f"{prefix}RevisionDown")
        or record.get(f"{prefix}RevisionsDown")
        or record.get(f"{prefix}_revision_down")
    )
    previous = _to_float(
        record.get(f"{prefix}Previous")
        or record.get(f"{prefix}Prior")
        or record.get(f"previous{prefix[0].upper()}{prefix[1:]}")
    )
    current = _to_float(
        record.get(prefix)
        or record.get(f"{prefix}Estimate")
        or record.get(f"{prefix}_estimate")
    )

    if up is not None or down is not None:
        positive = up or 0.0
        negative = down or 0.0
        total = positive + negative
        if total <= 0:
            return 0.0
        return (positive - negative) / total

    if current is not None and previous not in (None, 0.0):
        return (current - previous) / abs(previous)

    return None


def _confirmation_component(direction: float | None, max_points: float, *, cap: float = 0.25) -> float | None:
    if direction is None:
        return None
    bounded = max(-cap, min(cap, direction))
    normalized = (bounded + cap) / (2 * cap)
    return round(normalized * max_points, 2)


@dataclass(frozen=True)
class AlphaVantageConfirmation:
    ticker: str
    status: str
    confirmation_score: float | None
    fundamentally_confirmed: bool | None
    data_confidence: str
    details: dict[str, Any] = field(default_factory=dict)
    raw_payload_hash: str = ""


def parse_earnings_estimates(ticker: str, payload: dict[str, Any]) -> AlphaVantageConfirmation:
    quarterly_records = _extract_records(payload, "quarterlyEstimates", "quarterly_estimates")
    annual_records = _extract_records(payload, "annualEstimates", "annual_estimates")
    records = _latest_forward_records(quarterly_records, limit=2)

    if not records and not annual_records:
        return AlphaVantageConfirmation(
            ticker=ticker,
            status="UNAVAILABLE",
            confirmation_score=None,
            fundamentally_confirmed=None,
            data_confidence="low",
            details={"reason": "No forward estimate records present."},
            raw_payload_hash=_payload_hash(payload),
        )

    primary = records[0] if records else (annual_records[0] if annual_records else {})
    analyst_count = _to_int(
        primary.get("numberOfAnalysts")
        or primary.get("analystCount")
        or primary.get("analysts")
    )
    eps_direction = _revision_direction(primary, "epsEstimate")
    revenue_direction = _revision_direction(primary, "revenueEstimate")

    analyst_component = None
    if analyst_count is not None:
        analyst_component = round(min(15.0, max(0.0, analyst_count / 20.0 * 15.0)), 2)

    eps_component = _confirmation_component(eps_direction, 40.0)
    revenue_component = _confirmation_component(revenue_direction, 25.0)

    consistency_component = None
    if eps_direction is not None or revenue_direction is not None:
        positive_signals = sum(
            direction is not None and direction > 0 for direction in (eps_direction, revenue_direction)
        )
        negative_signals = sum(
            direction is not None and direction < 0 for direction in (eps_direction, revenue_direction)
        )
        if positive_signals and not negative_signals:
            consistency_component = 20.0
        elif positive_signals and negative_signals:
            consistency_component = 10.0
        elif negative_signals and not positive_signals:
            consistency_component = 0.0

    components = [eps_component, revenue_component, analyst_component, consistency_component]
    numeric_components = [component for component in components if component is not None]
    confirmation_score = round(sum(numeric_components), 2) if numeric_components else None

    confirmed: bool | None
    confidence = "medium"
    if confirmation_score is None:
        confirmed = None
        confidence = "low"
    elif confirmation_score >= 65.0:
        confirmed = True
        confidence = "high"
    elif confirmation_score <= 35.0:
        confirmed = False
        confidence = "medium"
    else:
        confirmed = None
        confidence = "medium"

    details = {
        "analyst_count": analyst_count,
        "eps_revision_direction": eps_direction,
        "revenue_revision_direction": revenue_direction,
        "eps_confirmation_component": eps_component,
        "revenue_confirmation_component": revenue_component,
        "analyst_confirmation_component": analyst_component,
        "consistency_confirmation_component": consistency_component,
        "quarterly_estimates_used": records,
        "annual_estimates_available": len(annual_records),
    }

    return AlphaVantageConfirmation(
        ticker=ticker,
        status="ENRICHED",
        confirmation_score=confirmation_score,
        fundamentally_confirmed=confirmed,
        data_confidence=confidence,
        details=details,
        raw_payload_hash=_payload_hash(payload),
    )


class AlphaVantageClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
        max_calls: int = 20,
        retry_limit: int = 1,
        sleep_seconds: float = 0.0,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("ALPHA_VANTAGE_FREE", "")).strip()
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.max_calls = max(0, int(max_calls))
        self.retry_limit = max(0, int(retry_limit))
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.calls_made = 0
        self._cache: dict[str, AlphaVantageConfirmation] = {}

    def fetch_earnings_estimates(self, ticker: str) -> AlphaVantageConfirmation:
        ticker = _clean_text(ticker).upper()
        cached = self._cache.get(ticker)
        if cached is not None:
            return cached

        if not self.api_key:
            result = AlphaVantageConfirmation(
                ticker=ticker,
                status="SKIPPED_NO_KEY",
                confirmation_score=None,
                fundamentally_confirmed=None,
                data_confidence="medium",
                details={},
                raw_payload_hash="",
            )
            self._cache[ticker] = result
            return result

        if self.calls_made >= self.max_calls:
            result = AlphaVantageConfirmation(
                ticker=ticker,
                status="SKIPPED_BUDGET",
                confirmation_score=None,
                fundamentally_confirmed=None,
                data_confidence="medium",
                details={"reason": "Daily Alpha Vantage budget exhausted."},
                raw_payload_hash="",
            )
            self._cache[ticker] = result
            return result

        params = {
            "function": "EARNINGS_ESTIMATES",
            "symbol": ticker,
            "apikey": self.api_key,
        }

        last_status = "UNAVAILABLE"
        for attempt in range(self.retry_limit + 1):
            try:
                self.calls_made += 1
                response = self.session.get(
                    BASE_URL,
                    params=params,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except requests.Timeout:
                last_status = "TIMEOUT"
                if attempt >= self.retry_limit:
                    break
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
                continue
            except requests.RequestException as exc:
                last_status = "REQUEST_ERROR"
                logger.warning(
                    "Alpha Vantage request failed for %s: %s",
                    ticker,
                    exc.__class__.__name__,
                )
                if attempt >= self.retry_limit:
                    break
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
                continue
            except ValueError:
                last_status = "INVALID_JSON"
                break

            if not isinstance(payload, dict):
                last_status = "INVALID_PAYLOAD"
                break

            if payload.get("Note") or payload.get("Information"):
                result = AlphaVantageConfirmation(
                    ticker=ticker,
                    status="RATE_LIMITED",
                    confirmation_score=None,
                    fundamentally_confirmed=None,
                    data_confidence="low",
                    details={"message": _clean_text(payload.get("Note") or payload.get("Information"))[:200]},
                    raw_payload_hash=_payload_hash(payload),
                )
                self._cache[ticker] = result
                return result

            if payload.get("Error Message"):
                result = AlphaVantageConfirmation(
                    ticker=ticker,
                    status="API_ERROR",
                    confirmation_score=None,
                    fundamentally_confirmed=None,
                    data_confidence="low",
                    details={"message": _clean_text(payload.get("Error Message"))[:200]},
                    raw_payload_hash=_payload_hash(payload),
                )
                self._cache[ticker] = result
                return result

            result = parse_earnings_estimates(ticker, payload)
            self._cache[ticker] = result
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
            return result

        result = AlphaVantageConfirmation(
            ticker=ticker,
            status=last_status,
            confirmation_score=None,
            fundamentally_confirmed=None,
            data_confidence="low",
            details={},
            raw_payload_hash="",
        )
        self._cache[ticker] = result
        return result
