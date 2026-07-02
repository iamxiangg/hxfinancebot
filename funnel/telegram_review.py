from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests


CALLBACK_PREFIX = "hxv2"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewAction:
    action: str
    candidate_id: str


def build_callback_data(action: str, candidate_id: str) -> str:
    return f"{CALLBACK_PREFIX}:{action.lower()}:{candidate_id}"


def parse_callback_data(data: str) -> ReviewAction | None:
    parts = str(data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None

    action = parts[1].strip().lower()
    candidate_id = parts[2].strip()
    if action not in {"approve", "reject", "archive"} or not candidate_id:
        return None

    return ReviewAction(action=action, candidate_id=candidate_id)


def candidate_id_for_ticker(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"cand-{normalized}-{digest}"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _present(value: Any) -> bool:
    return _clean_text(value) != ""


def _source_label(value: str) -> str:
    mapping = {
        "congress": "Political Disclosures",
        "insider": "Corporate Insider",
        "vpma": "VPMA / PEAD",
        "manual": "Manual",
    }
    return mapping.get(value.strip().lower(), value.strip())


def _ratio_percent_line(label: str, value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return f"- {label}: {float(text) * 100:.1f}%"
    except (TypeError, ValueError):
        return f"- {label}: {text}"


def _value_line(label: str, value: Any, *, prefix: str = "- ") -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return f"{prefix}{label}: {text}"


def _decimal_line(label: str, value: Any, *, places: int = 2) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return f"- {label}: {text}"
    return f"- {label}: {number:.{places}f}"


def _judgment_block(candidate: dict[str, Any]) -> list[str]:
    lane = _clean_text(candidate.get("Decision Lane"))
    if not lane:
        return []

    lines = ["JUDGMENT LAYER", f"- Suggested lane: {lane}"]

    attention = _clean_text(candidate.get("Attention Family"))
    if attention:
        lines.append(f"- Attention family: {attention}")

    technical = _clean_text(candidate.get("Technical Confirmation"))
    ownership = _clean_text(candidate.get("Ownership Confirmation"))
    forward = _clean_text(candidate.get("Forward Confirmation"))
    confirmation_bits = []
    if technical:
        confirmation_bits.append(f"technical {technical.lower()}")
    if ownership:
        confirmation_bits.append(f"ownership {ownership.lower()}")
    if forward:
        confirmation_bits.append(f"forward {forward.lower()}")
    if confirmation_bits:
        lines.append(f"- Confirmation: {' | '.join(confirmation_bits)}")

    risks = _clean_text(candidate.get("Risk Flags"))
    if risks:
        lines.append(f"- Breakers / gaps: {risks}")

    lane_reason = _clean_text(candidate.get("Decision Lane Reason"))
    if lane_reason:
        lines.append(f"- Why this lane: {lane_reason}")

    return lines


def build_review_message(candidate: dict[str, Any]) -> str:
    source_values = [
        _source_label(part)
        for part in str(candidate.get("Source") or "").split(",")
        if part.strip()
    ]
    lines = [f"Review candidate: {candidate.get('Ticker', '')}"]

    company = candidate.get("Company Name")
    if company:
        lines.insert(1, f"Company: {company}")

    if source_values:
        lines.append(f"Sources: {', '.join(source_values)}")

    corroboration = _clean_text(candidate.get("Corroboration Level"))
    if corroboration:
        lines.append(f"Corroboration: {corroboration}")

    ai_summary = candidate.get("AI Quality Summary")
    if ai_summary:
        lines.append(f"AI draft: {ai_summary}")

    red_flags = candidate.get("AI Red Flags")
    if red_flags:
        lines.append(f"Red flags: {red_flags}")

    reason = candidate.get("Discovery Reason")
    if reason:
        lines.append(f"Signal: {reason}")

    judgment_lines = _judgment_block(candidate)
    if judgment_lines:
        lines.append("")
        lines.extend(judgment_lines)

    lines.append("")
    lines.append("BTD BASIC GATE")
    lines.append(f"- Status: {_clean_text(candidate.get('BTD Gate')) or _clean_text(candidate.get('Status'))}")
    ratio = _clean_text(candidate.get("BTD Ratio")) or _clean_text(candidate.get("BTD Score"))
    maybe_ratio_line = _decimal_line("BTD ratio", ratio, places=2)
    if maybe_ratio_line:
        lines.append(maybe_ratio_line)
    for maybe_line in (
        _ratio_percent_line("Gross margin", candidate.get("Gross Margin")),
        _ratio_percent_line("Revenue growth", candidate.get("Revenue Growth")),
    ):
        if maybe_line:
            lines.append(maybe_line)
    gate_reason = _clean_text(candidate.get("BTD Gate Reason"))
    if gate_reason:
        lines.append(f"- Note: {gate_reason}")

    congress_unique_members = candidate.get("Congress Unique Members")
    congress_recent_cluster = candidate.get("Congress Recent Cluster Members")
    congress_active_purchases = candidate.get("Congress Active Purchases")
    congress_member_names = candidate.get("Congress Member Names")
    if any(
        _present(value)
        for value in (
            congress_unique_members,
            congress_recent_cluster,
            congress_active_purchases,
            congress_member_names,
        )
    ):
        lines.append("Political disclosure breadth:")
        if _present(congress_unique_members):
            lines.append(f"- Unique members represented: {congress_unique_members}")
        if _present(congress_recent_cluster):
            lines.append(f"- Recent cluster members: {congress_recent_cluster}")
        if _present(congress_active_purchases):
            lines.append(f"- Active purchases: {congress_active_purchases}")
        if _present(congress_member_names):
            lines.append(f"- Members represented: {congress_member_names}")

    insider_fields = (
        candidate.get("Insider Total Score"),
        candidate.get("Insider Conviction"),
        candidate.get("Insider Economic Commitment"),
        candidate.get("Insider Market Context"),
        candidate.get("Insider Unique Insiders"),
        candidate.get("Insider Roles"),
        candidate.get("Insider Aggregate Purchase"),
        candidate.get("Insider Cluster Span Days"),
        candidate.get("Insider Weighted Purchase Price"),
        candidate.get("Insider Entry State"),
    )
    if any(_present(value) for value in insider_fields):
        lines.append("Corporate insider:")
        for maybe_line in (
            _value_line("Total score", candidate.get("Insider Total Score")),
            _value_line("Insider conviction", candidate.get("Insider Conviction")),
            _value_line("Economic commitment", candidate.get("Insider Economic Commitment")),
            _value_line("Market context", candidate.get("Insider Market Context")),
            _value_line("Unique insiders", candidate.get("Insider Unique Insiders")),
            _value_line("Roles", candidate.get("Insider Roles")),
            _value_line("Aggregate purchase", candidate.get("Insider Aggregate Purchase")),
            _value_line("Cluster span", candidate.get("Insider Cluster Span Days")),
            _value_line("Weighted purchase price", candidate.get("Insider Weighted Purchase Price")),
            _value_line("Entry state", candidate.get("Insider Entry State")),
        ):
            if maybe_line:
                lines.append(maybe_line)

    summary = candidate.get("BTD Summary")
    if summary:
        lines.append("")
        lines.append(f"BTD summary: {summary}")

    lines.append("")
    lines.append("BTD basic economic gate passed. Manual long-term review remains required.")

    lines.append("")
    lines.append("Choose an action below. Approval adds the ticker to Stock Summary USD.")
    return "\n".join(str(line) for line in lines)


def send_candidate_review(
    candidate: dict[str, Any],
    *,
    token: str | None = None,
    chat_id: str | None = None,
) -> str:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    candidate_id = str(candidate.get("Candidate ID") or "").strip()
    if not candidate_id:
        raise ValueError("Candidate ID is required")

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": build_review_message(candidate),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Approve",
                            "callback_data": build_callback_data("approve", candidate_id),
                        },
                        {
                            "text": "Reject",
                            "callback_data": build_callback_data("reject", candidate_id),
                        },
                        {
                            "text": "Archive",
                            "callback_data": build_callback_data("archive", candidate_id),
                        },
                    ]
                ]
            },
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(json.dumps(payload, sort_keys=True))
    return str(payload.get("result", {}).get("message_id", ""))


def send_telegram_text(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return bool(payload.get("ok"))
    except requests.RequestException as exc:
        logger.warning("Telegram confirmation message failed: %r", exc)
        return False


def get_updates(
    offset: int,
    *,
    token: str | None = None,
) -> list[dict[str, Any]]:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    response = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["callback_query"]),
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(json.dumps(payload, sort_keys=True))
    return list(payload.get("result", []))


def answer_callback(
    callback_query_id: str,
    text: str,
    *,
    token: str | None = None,
) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not callback_query_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text[:180]},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram callback acknowledgement failed: %r", exc)
        return False
