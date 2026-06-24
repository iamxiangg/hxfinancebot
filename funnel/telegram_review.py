from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import requests


CALLBACK_PREFIX = "hxv2"


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


def build_review_message(candidate: dict[str, Any]) -> str:
    lines = [
        f"Review candidate: ${candidate.get('Ticker', '')}",
        f"Status: {candidate.get('Status', '')}",
        f"Funnel: {candidate.get('Funnel Score', '')}",
        f"BTD: {candidate.get('BTD Score', '')}",
    ]

    company = candidate.get("Company Name")
    if company:
        lines.insert(1, f"Company: {company}")

    summary = candidate.get("BTD Summary")
    if summary:
        lines.append(f"BTD summary: {summary}")

    ai_summary = candidate.get("AI Quality Summary")
    if ai_summary:
        lines.append(f"AI draft: {ai_summary}")

    red_flags = candidate.get("AI Red Flags")
    if red_flags:
        lines.append(f"Red flags: {red_flags}")

    reason = candidate.get("Discovery Reason")
    if reason:
        lines.append(f"Signal: {reason}")

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
) -> None:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not callback_query_id:
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id, "text": text[:180]},
        timeout=15,
    ).raise_for_status()
