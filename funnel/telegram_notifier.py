# VERSION: 2026-06-23-PRODUCTION-TELEGRAM-NOTIFIER-1

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable

import requests


TEXT_LIMIT = 3900

MATERIAL_FIELDS = (
    "Latest Classification",
    "Review Route",
    "Candidate Status",
    "Flow",
)


class TelegramNotificationError(RuntimeError):
    """Raised when Telegram cannot be configured or reached."""


def _text(value: Any) -> str:
    """Return a stripped string."""
    return "" if value is None else str(value).strip()


def _ticker(value: Any) -> str:
    """Return a normalised ticker."""
    return _text(value).upper()


def _positive_int(
    name: str,
    default: int,
) -> int:
    """Read a positive integer from an environment variable."""
    try:
        value = int(
            _text(
                os.getenv(
                    name,
                    str(default),
                )
            )
        )
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer."
        ) from exc

    if value < 1:
        raise ValueError(
            f"{name} must be at least 1."
        )

    return value


def _number(value: Any) -> str:
    """Format a finite numeric value."""
    try:
        number = float(value)
    except (
        TypeError,
        ValueError,
    ):
        return "-"

    return (
        f"{number:.1f}"
        if math.isfinite(number)
        else "-"
    )


def _index_current(
    records: list[dict[str, Any]],
    label: str,
) -> dict[str, dict[str, Any]]:
    """Index current-run records by ticker."""
    output: dict[
        str,
        dict[str, Any],
    ] = {}

    for record in records:
        if (
            _text(
                record.get(
                    "Current Run"
                )
            ).upper()
            != "YES"
        ):
            continue

        ticker = _ticker(
            record.get(
                "Ticker"
            )
        )

        if not ticker:
            continue

        if ticker in output:
            raise RuntimeError(
                f"Duplicate ticker {ticker!r} "
                f"in {label}."
            )

        output[ticker] = record

    return output


def analyse_funnel_changes(
    old_funnel_records: list[dict[str, Any]],
    new_funnel_records: list[dict[str, Any]],
    old_pending_records: list[dict[str, Any]],
    new_pending_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Compare pre-write and post-write current snapshots by ticker.

    Signal IDs and observation timestamps are deliberately ignored
    because they normally change on every scanner run.
    """
    old_funnel = _index_current(
        old_funnel_records,
        "old funnel",
    )

    new_funnel = _index_current(
        new_funnel_records,
        "new funnel",
    )

    old_pending = _index_current(
        old_pending_records,
        "old pending",
    )

    new_pending = _index_current(
        new_pending_records,
        "new pending",
    )

    new_tickers = sorted(
        set(new_funnel)
        - set(old_funnel)
    )

    dropped_tickers = sorted(
        set(old_funnel)
        - set(new_funnel)
    )

    new_pending_tickers = sorted(
        set(new_pending)
        - set(old_pending)
    )

    resolved_pending_tickers = sorted(
        set(old_pending)
        - set(new_pending)
    )

    changed: list[
        dict[str, Any]
    ] = []

    for ticker in sorted(
        set(old_funnel)
        & set(new_funnel)
    ):
        old_record = old_funnel[ticker]
        new_record = new_funnel[ticker]

        changed_fields = [
            field
            for field in MATERIAL_FIELDS
            if _text(
                old_record.get(field)
            )
            != _text(
                new_record.get(field)
            )
        ]

        if changed_fields:
            changed.append(
                {
                    "ticker": ticker,
                    "fields": changed_fields,
                    "old": old_record,
                    "new": new_record,
                }
            )

    promotion_ready = sorted(
        [
            record
            for record in new_pending_records
            if (
                _text(
                    record.get(
                        "Current Run"
                    )
                ).upper()
                == "YES"
            )
            and (
                _text(
                    record.get(
                        "Validation Status"
                    )
                ).upper()
                == "REVIEWED"
            )
            and (
                _text(
                    record.get(
                        "Add to Stock Summary USD?"
                    )
                ).upper()
                == "YES"
            )
        ],
        key=lambda record: _ticker(
            record.get(
                "Ticker"
            )
        ),
    )

    material_change_count = (
        len(new_tickers)
        + len(dropped_tickers)
        + len(changed)
        + len(new_pending_tickers)
        + len(resolved_pending_tickers)
    )

    return {
        "new_signals": [
            new_funnel[ticker]
            for ticker in new_tickers
        ],
        "changed_signals": changed,
        "dropped_signals": [
            old_funnel[ticker]
            for ticker in dropped_tickers
        ],
        "new_pending": [
            new_pending[ticker]
            for ticker in new_pending_tickers
        ],
        "resolved_pending": [
            old_pending[ticker]
            for ticker in resolved_pending_tickers
        ],
        "promotion_ready": promotion_ready,
        "current_signal_count": len(
            new_funnel
        ),
        "current_pending_count": len(
            new_pending
        ),
        "material_change_count": (
            material_change_count
        ),
    }


def _signal_line(
    record: dict[str, Any],
) -> str:
    """Format one signal for Telegram."""
    ticker = (
        _ticker(
            record.get(
                "Ticker"
            )
        )
        or "?"
    )

    classification = (
        _text(
            record.get(
                "Latest Classification"
            )
            or record.get(
                "Classification"
            )
        ).upper()
        or "UNKNOWN"
    )

    score = _number(
        record.get(
            "Latest Score"
        )
        or record.get(
            "Score"
        )
    )

    entry = _number(
        record.get(
            "Entry Quality"
        )
    )

    flow = _text(
        record.get(
            "Flow"
        )
    )

    line = (
        f"${ticker} | {classification} | "
        f"C{score}/E{entry}"
    )

    if flow:
        line += f" | {flow}"

    return line


def _changed_line(
    change: dict[str, Any],
) -> str:
    """Format one material signal change."""
    labels = {
        "Latest Classification": "class",
        "Review Route": "route",
        "Candidate Status": "status",
        "Flow": "flow",
    }

    parts = [
        (
            f"{labels.get(field, field)} "
            f"{_text(change['old'].get(field)) or '-'} "
            f"→ "
            f"{_text(change['new'].get(field)) or '-'}"
        )
        for field in change["fields"]
    ]

    return (
        f"${change['ticker']} | "
        + "; ".join(parts)
    )


def _section(
    lines: list[str],
    title: str,
    items: list[Any],
    formatter: Callable[[Any], str],
    limit: int = 8,
) -> None:
    """Append a limited Telegram message section."""
    if not items:
        return

    lines.extend(
        [
            "",
            title,
        ]
    )

    lines.extend(
        formatter(item)
        for item in items[:limit]
    )

    if len(items) > limit:
        lines.append(
            f"…and {len(items) - limit} more"
        )


def build_funnel_message(
    run_receipt: dict[str, Any],
    changes: dict[str, Any],
    *,
    test_mode: bool = False,
) -> str:
    """Build one plain-text HX Funnel Telegram update."""
    title = (
        "🧪 HX FUNNEL TELEGRAM TEST"
        if test_mode
        else "📊 HX FUNNEL PRODUCTION UPDATE"
    )

    lines = [
        title,
        "",
        (
            "Analysed: "
            f"{run_receipt.get('congress_tickers_analysed', '-')}"
        ),
        (
            "Current signals: "
            f"{changes['current_signal_count']}"
        ),
        (
            "New signals: "
            f"{len(changes['new_signals'])}"
        ),
        (
            "Changed signals: "
            f"{len(changes['changed_signals'])}"
        ),
        (
            "Dropped to historical: "
            f"{len(changes['dropped_signals'])}"
        ),
        (
            "Pending review: "
            f"{changes['current_pending_count']}"
        ),
        (
            "New pending: "
            f"{len(changes['new_pending'])}"
        ),
        (
            "Pending resolved: "
            f"{len(changes['resolved_pending'])}"
        ),
    ]

    _section(
        lines,
        "🆕 NEW SIGNALS",
        changes["new_signals"],
        _signal_line,
    )

    _section(
        lines,
        "🔄 MATERIAL CHANGES",
        changes["changed_signals"],
        _changed_line,
    )

    _section(
        lines,
        "🕘 MOVED TO HISTORICAL",
        changes["dropped_signals"],
        lambda record: (
            f"${_ticker(record.get('Ticker'))} "
            "| Current → Historical"
        ),
    )

    _section(
        lines,
        "🧾 NEW PENDING REVIEW",
        changes["new_pending"],
        _signal_line,
    )

    _section(
        lines,
        "✅ PROMOTION READY",
        changes["promotion_ready"],
        lambda record: (
            f"${_ticker(record.get('Ticker'))} "
            "| REVIEWED | APPROVED"
        ),
    )

    if (
        test_mode
        and changes[
            "material_change_count"
        ]
        == 0
    ):
        lines.extend(
            [
                "",
                (
                    "No material funnel change was detected; "
                    "this message was forced by TEST mode."
                ),
            ]
        )

    return "\n".join(
        lines
    ).strip()


def chunk_message(
    text: str,
    limit: int = TEXT_LIMIT,
) -> list[str]:
    """Split text on line boundaries below Telegram's hard limit."""
    cleaned = text.strip()

    if not cleaned:
        return []

    chunks: list[str] = []
    current = ""

    for original_line in cleaned.splitlines():
        line = original_line.rstrip()

        candidate = (
            line
            if not current
            else f"{current}\n{line}"
        )

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(
                current
            )

            current = ""

        while len(line) > limit:
            chunks.append(
                line[:limit]
            )

            line = line[limit:]

        current = line

    if current:
        chunks.append(
            current
        )

    return chunks


def _retry_delay(
    payload: dict[str, Any] | None,
    attempt: int,
) -> int:
    """Use Telegram retry_after or an exponential delay."""
    try:
        retry_after = int(
            (
                payload
                or {}
            ).get(
                "parameters",
                {},
            ).get(
                "retry_after"
            )
        )

        if retry_after > 0:
            return retry_after

    except (
        AttributeError,
        TypeError,
        ValueError,
    ):
        pass

    return min(
        30,
        2 ** attempt,
    )


def _send_one(
    token: str,
    chat_id: str,
    text: str,
    attempts: int,
    timeout: int,
) -> int:
    """Send one Telegram message with retries."""
    endpoint = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    last_error = (
        "Unknown Telegram error"
    )

    for attempt in range(
        1,
        attempts + 1,
    ):
        payload: dict[
            str,
            Any,
        ] | None = None

        try:
            response = requests.post(
                endpoint,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "link_preview_options": {
                        "is_disabled": True,
                    },
                },
                timeout=timeout,
            )

            try:
                parsed = response.json()

                payload = (
                    parsed
                    if isinstance(
                        parsed,
                        dict,
                    )
                    else None
                )

            except ValueError:
                payload = None

            if (
                response.ok
                and payload
                and payload.get(
                    "ok"
                )
                is True
            ):
                return attempt

            description = (
                _text(
                    (
                        payload
                        or {}
                    ).get(
                        "description"
                    )
                )
                or _text(
                    response.text
                )[:500]
            )

            last_error = (
                f"Telegram HTTP "
                f"{response.status_code}: "
                f"{description}"
            )

            if (
                response.status_code
                != 429
                and response.status_code
                < 500
            ):
                break

        except requests.RequestException as exc:
            last_error = (
                "Telegram request error: "
                f"{exc.__class__.__name__}: "
                f"{exc}"
            )

        if attempt < attempts:
            time.sleep(
                _retry_delay(
                    payload,
                    attempt,
                )
            )

    raise TelegramNotificationError(
        last_error
    )


def send_telegram_text(
    text: str,
) -> dict[str, Any]:
    """Send plain-text messages and return an audit result."""
    token = _text(
        os.getenv(
            "TELEGRAM_BOT_TOKEN"
        )
    )

    chat_id = _text(
        os.getenv(
            "TELEGRAM_CHAT_ID"
        )
    )

    if not token or not chat_id:
        raise TelegramNotificationError(
            "TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID are required."
        )

    chunks = chunk_message(
        text
    )

    if not chunks:
        return {
            "status": "SKIPPED_EMPTY",
            "message_count": 0,
            "attempts_total": 0,
        }

    attempts = _positive_int(
        "TELEGRAM_MAX_ATTEMPTS",
        3,
    )

    timeout = _positive_int(
        "TELEGRAM_TIMEOUT_SECONDS",
        30,
    )

    attempts_total = 0

    for index, chunk in enumerate(
        chunks
    ):
        attempts_total += _send_one(
            token,
            chat_id,
            chunk,
            attempts,
            timeout,
        )

        if index < len(chunks) - 1:
            time.sleep(
                1
            )

    return {
        "status": "SENT",
        "message_count": len(
            chunks
        ),
        "attempts_total": (
            attempts_total
        ),
    }