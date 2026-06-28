from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


CALLBACK_PREFIX = "hxv2"
CALLBACK_PREFIX_V3 = "hx3"
REVIEW_EXPIRY_HOURS = 72
CALLBACK_MAX_BYTES = 64
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback parsing (supports both legacy hxv2 and current hx3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewAction:
    action: str
    candidate_id: str
    review_id: str = ""


@dataclass(frozen=True)
class CallbackAuth:
    """Validated callback identity that must be enforced server-side."""
    user_id: int
    chat_id: int
    message_id: int


def _allowed_user_ids() -> set[int]:
    raw = str(os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")).strip()
    if not raw:
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            logger.warning("Invalid TELEGRAM_ALLOWED_USER_IDS entry: %r", part)
    return result


def _allowed_chat_ids() -> set[int]:
    raw = str(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")).strip()
    default_chat = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    ids: set[int] = set()
    for text in raw.split(","):
        text = text.strip()
        if not text:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            logger.warning("Invalid TELEGRAM_ALLOWED_CHAT_IDS entry: %r", text)
    if not ids and default_chat:
        try:
            ids.add(int(default_chat))
        except ValueError:
            pass
    return ids


def _validate_callback_auth(callback_query: dict[str, Any]) -> CallbackAuth | None:
    """Fail closed: return None if any check fails."""

    allowed_users = _allowed_user_ids()
    if not allowed_users:
        logger.error("TELEGRAM_ALLOWED_USER_IDS is missing or empty; callback writes disabled.")
        return None

    sender = callback_query.get("from") or {}
    try:
        user_id = int(sender.get("id", 0))
    except (TypeError, ValueError):
        user_id = 0

    if user_id not in allowed_users:
        logger.warning("Callback from unauthorised user %d", user_id)
        return None

    message = callback_query.get("message") or {}
    if not message:
        logger.warning("Callback has no attached message.")
        return None

    allowed_chats = _allowed_chat_ids()
    try:
        chat_id = int(message.get("chat", {}).get("id", 0))
    except (TypeError, ValueError):
        chat_id = 0

    if chat_id not in allowed_chats:
        logger.warning("Callback from unauthorised chat %d", chat_id)
        return None

    try:
        message_id = int(message.get("message_id", 0))
    except (TypeError, ValueError):
        message_id = 0

    if message_id <= 0:
        logger.warning("Callback has invalid message_id.")
        return None

    return CallbackAuth(user_id=user_id, chat_id=chat_id, message_id=message_id)


def build_callback_data(action: str, candidate_id: str) -> str:
    """Legacy hxv2 builder – kept for existing non-actionable senders.
    DO NOT use for new review cards."""
    return f"{CALLBACK_PREFIX}:{action.lower()}:{candidate_id}"


def build_callback_data_v3(action: str, review_id: str) -> str:
    """Build an hx3 callback: hx3:a:<review_id>"""
    return f"{CALLBACK_PREFIX_V3}:{action[0].lower()}:{review_id}"


def parse_callback_data(data: str) -> ReviewAction | None:
    """Parse callback data, returning None for unknown/malformed formats.
    Legacy hxv2 callbacks return a ReviewAction with empty review_id for
    safe rejection upstream."""
    text = str(data or "").strip()
    if not text:
        return None

    parts = text.split(":", 2)
    if len(parts) != 3:
        return None

    prefix = parts[0]

    # --- Legacy hxv2 ---
    if prefix == CALLBACK_PREFIX:
        action = parts[1].strip().lower()
        candidate_id = parts[2].strip()
        if action not in {"approve", "reject", "archive"} or not candidate_id:
            return None
        return ReviewAction(action=action, candidate_id=candidate_id, review_id="")

    # --- Current hx3 ---
    if prefix == CALLBACK_PREFIX_V3:
        action_code = parts[1].strip().lower()
        review_id = parts[2].strip()
        action_map = {"a": "approve", "r": "reject", "x": "archive"}
        action = action_map.get(action_code, "")
        if not action or not review_id:
            return None
        return ReviewAction(action=action, candidate_id="", review_id=review_id)

    return None


def is_legacy_callback(data: str) -> bool:
    text = str(data or "").strip()
    return text.startswith(f"{CALLBACK_PREFIX}:")


# ---------------------------------------------------------------------------
# Review ID helpers
# ---------------------------------------------------------------------------

def new_review_id() -> str:
    return secrets.token_urlsafe(16)


def review_expires_at(issued_at: datetime | None = None) -> str:
    stamp = (issued_at or datetime.now(timezone.utc)) + timedelta(hours=REVIEW_EXPIRY_HOURS)
    return stamp.isoformat()


def is_review_expired(expires_at_str: str) -> bool:
    try:
        expiry = datetime.fromisoformat(expires_at_str)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) > expiry


# ---------------------------------------------------------------------------
# Candidate snapshot hash (Workstream A6)
# ---------------------------------------------------------------------------

_SNAPSHOT_FIELDS = (
    "Candidate ID",
    "Ticker",
    "Status",
    "Active?",
    "Telegram Eligible",
    "BTD Gate",
    "BTD Ratio",
    "BTD Last Updated",
    "Supporting Signal IDs",
    "Last Seen",
)


def compute_snapshot_hash(candidate: dict[str, Any]) -> str:
    subset = {}
    for field in _SNAPSHOT_FIELDS:
        value = candidate.get(field, "")
        if isinstance(value, (datetime,)):
            value = value.isoformat()
        subset[field] = str(value)
    canonical = json.dumps(subset, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_snapshot(candidate: dict[str, Any], expected_hash: str) -> bool:
    return compute_snapshot_hash(candidate) == expected_hash


# ---------------------------------------------------------------------------
# Candidate eligibility checks (workstream A5, A6)
# ---------------------------------------------------------------------------

def _is_pass_or_bypass(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return text in {"PASS", "BYPASSED_MANUAL"}


def _is_yes(value: Any) -> bool:
    return str(value or "").strip().upper() == "YES"


def _is_notified_or_review(status: str) -> bool:
    return str(status or "").strip().upper() in {"NOTIFIED", "REVIEW"}


def candidate_is_eligible_for_review(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Return (eligible, reason). A candidate must PASS all checks."""
    status = str(candidate.get("Status") or "").strip().upper()
    active = str(candidate.get("Active?") or "").strip().upper()
    telegram_eligible = str(candidate.get("Telegram Eligible") or "").strip().upper()
    btd_gate = str(candidate.get("BTD Gate") or "").strip().upper()

    if not _is_notified_or_review(status):
        return False, f"status is {status}, expected NOTIFIED or REVIEW"
    if active != "YES":
        return False, "Active? is not YES"
    if telegram_eligible != "YES":
        return False, "Telegram Eligible is not YES"
    if not _is_pass_or_bypass(btd_gate):
        return False, f"BTD Gate is {btd_gate}, expected PASS or BYPASSED_MANUAL"
    return True, ""


# ---------------------------------------------------------------------------
# Legacy candidate-ID helpers
# ---------------------------------------------------------------------------

def candidate_id_for_ticker(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"cand-{normalized}-{digest}"


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

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

    lines.append("")
    lines.append("BTD BASIC GATE")
    lines.append(f"- Status: {_clean_text(candidate.get('BTD Gate')) or _clean_text(candidate.get('Status'))}")
    ratio = _clean_text(candidate.get("BTD Ratio")) or _clean_text(candidate.get("BTD Score"))
    if ratio:
        lines.append(f"- BTD ratio: {ratio}")
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
    lines.append("Choose an action below. Approval records an approval request for controlled promotion.")
    return "\n".join(str(line) for line in lines)


# ---------------------------------------------------------------------------
# Outbox-style send (Workstream A7)
# ---------------------------------------------------------------------------

def send_candidate_review(
    candidate: dict[str, Any],
    *,
    token: str | None = None,
    chat_id: str | None = None,
) -> str:
    """Legacy send_candidate_review — kept for existing review_candidates.py flow.
    Still uses hxv2 callbacks for backward-compatible cards."""
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


def send_review_card_v3(
    candidate: dict[str, Any],
    review_id: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
) -> str:
    """Send a review card with hx3 callback and return the Telegram message ID."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

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
                            "callback_data": build_callback_data_v3("approve", review_id),
                        },
                        {
                            "text": "Reject",
                            "callback_data": build_callback_data_v3("reject", review_id),
                        },
                        {
                            "text": "Archive",
                            "callback_data": build_callback_data_v3("archive", review_id),
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


def edit_message_keyboard(
    chat_id_val: int,
    message_id_val: int,
    *,
    token: str | None = None,
) -> bool:
    """Remove the inline keyboard after a terminal decision."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id_val,
                "message_id": message_id_val,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to remove keyboard from message %d: %r", message_id_val, exc)
        return False


# ---------------------------------------------------------------------------
# Telegram text helpers
# ---------------------------------------------------------------------------

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
