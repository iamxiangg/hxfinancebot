from __future__ import annotations

import json
import os
from typing import Any

import requests

from funnel.telegram_review import build_review_keyboard, build_review_message


FEROLDI_SECTION_MAXIMUMS = {
    "Financials": 17,
    "Management": 10,
    "Stock": 11,
    "Overall": 38,
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> str:
    text = _clean(value)
    if not text:
        return "?"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def build_feroldi_block(candidate: dict[str, Any]) -> str:
    gate = _clean(candidate.get("Feroldi Gate")) or "PENDING"
    mode = (_clean(candidate.get("Feroldi Gate Mode")) or "OBSERVE").lower()

    financial_score = _number(candidate.get("Feroldi Financial Score"))
    financial_available = _clean(candidate.get("Feroldi Financial Available"))
    management_score = _number(candidate.get("Feroldi Management Score"))
    management_available = _number(candidate.get("Feroldi Management Available"))
    stock_score = _number(candidate.get("Feroldi Stock Score"))
    stock_available = _clean(candidate.get("Feroldi Stock Available"))
    overall_score = _number(candidate.get("Feroldi First Cut Score"))
    overall_available = _number(candidate.get("Feroldi Available Points"))
    equivalent = _number(candidate.get("Feroldi Equivalent Score"))

    financial_denominator = "17"
    if financial_available and financial_available not in {"17", "17.0"}:
        financial_denominator = f"{_number(financial_available)} available (max 17)"

    stock_denominator = "11"
    if stock_available and stock_available not in {"11", "11.0"}:
        stock_denominator = f"{_number(stock_available)} available (max 11)"

    management_denom = "10"
    if management_available and management_available not in ("10", "10.0"):
        management_denom = f"{_number(management_available)} available (max 10)"

    lines = [
        "FEROLDI FIRST-CUT",
        f"- Status: {gate} ({mode})",
        f"- Financials: {financial_score}/{financial_denominator}",
        f"- Management & culture: {management_score}/{management_denom}",
        f"- Stock: {stock_score}/{stock_denominator}",
        f"- Overall: {overall_score}/{overall_available} available",
        f"- Equivalent: {equivalent}/38",
    ]

    missing = _clean(candidate.get("Feroldi Missing Inputs"))
    if missing:
        lines.append(f"- Missing inputs: {missing}")

    reason = _clean(candidate.get("Feroldi Gate Reason"))
    if reason:
        lines.append(f"- Note: {reason}")

    return "\n".join(lines)


def build_review_message_with_feroldi(candidate: dict[str, Any]) -> str:
    base = build_review_message(candidate)
    block = build_feroldi_block(candidate)

    markers = (
        "\nPolitical disclosure breadth:",
        "\nCorporate insider:",
        "\n\nBTD summary:",
        "\n\nBTD basic economic gate passed.",
    )
    positions = [base.find(marker) for marker in markers if base.find(marker) >= 0]
    insertion_point = min(positions) if positions else len(base)

    return base[:insertion_point] + "\n\n" + block + base[insertion_point:]


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
            "text": build_review_message_with_feroldi(candidate),
            "reply_markup": build_review_keyboard(candidate),
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(json.dumps(payload, sort_keys=True))
    return str(payload.get("result", {}).get("message_id", ""))
