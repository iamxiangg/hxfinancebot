from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from funnel.congress_adapter import run_congress_adapter_detailed
from funnel.political_archive import load_political_archive_state, persist_digest_rows
from scanners.congress.engine import MODEL_VERSION
from tactical.congress_digest import chunk_digest, digest_log_rows, render_digest, write_digest_preview


TOKEN = str(os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
TZ = ZoneInfo("Asia/Singapore")
LOCK_FILE = Path("congress_bot.lock")
LOG_FILE = "congress_bot.log"
TG_LIMIT = 3800
MAX_ACTIONABLE = 8
MAX_WAIT = 6
MAX_RISK = 6
MAX_NEAREST = 5

logger = logging.getLogger("congress_bot")
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for handler in (logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")):
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _digest_preview_path() -> Path:
    audit_dir = str(os.getenv("CONGRESS_AUDIT_DIR", "")).strip()
    if audit_dir:
        path = Path(audit_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path / "political_digest_preview.txt"
    path = Path("funnel_output")
    path.mkdir(parents=True, exist_ok=True)
    return path / "political_digest_preview.txt"


def money(value: float) -> str:
    value = float(value or 0.0)
    sign, value = ("-", -value) if value < 0 else ("", value)
    if value >= 1e9:
        return f"{sign}${value / 1e9:.1f}b"
    if value >= 1e6:
        return f"{sign}${value / 1e6:.1f}m"
    if value >= 1e3:
        return f"{sign}${value / 1e3:.0f}k"
    return f"{sign}${value:.0f}"


def lock() -> bool:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - LOCK_FILE.stat().st_mtime > 3600:
                LOCK_FILE.unlink()
                return lock()
        except Exception:
            pass
        logger.error("Another run appears active: %s", LOCK_FILE)
        return False


def unlock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not remove lock: %s", exc)


def rank(result) -> tuple[float, float, float]:
    return result.conviction, result.entry, result.effective


def note(result) -> str:
    parts = []
    if result.role_relevance_score:
        parts.append(f"role relevance {result.role_relevance_score:.0f}/20")
    if result.committee_names:
        parts.append(f"top match {result.committee_names[0]}")
    elif result.agency_keys:
        parts.append(f"top match {result.agency_keys[0].replace('_', ' ')}")
    if result.call_mid:
        parts.append(f"Calls {money(result.call_mid)}, call bonus +{result.call_bonus:.0f}")
    if result.put_mid:
        parts.append(f"Puts {money(result.put_mid)}, penalty -{result.put_penalty:.0f}")
    if result.matched_sales:
        parts.append(f"{result.matched_sales} matched option sale")
    if result.unclear_sales:
        parts.append(f"{result.unclear_sales} unclear option sale")
    if result.signal_trigger == "late_disclosure":
        parts.append("late disclosure trigger")
    return ", ".join(parts)


def line(result) -> str:
    prefix = "BUYER CLUSTER " if result.cluster_buyers >= 2 else ""
    extra = f" | {note(result)}" if note(result) else ""
    names = ", ".join(result.names[:4])
    branch_mix = " + ".join(part.title() for part in result.branches) if result.branches else "Unknown"
    intent = ", ".join(result.asset_intent_classes[:2]).replace("_", " ").title() if result.asset_intent_classes else "Unknown"
    return (
        f"{prefix}${result.ticker} | C{result.conviction:.0f}/E{result.entry:.0f} | "
        f"Active {money(result.active_amount_mid)} [{money(result.active_amount_low)}-{money(result.active_amount_high)}] | "
        f"{result.buyers} filers | {branch_mix} | {intent} | {result.cluster_type.replace('_', ' ').title()} | "
        f"Wtd age {result.weighted_age:.0f}d | Since trade {result.weighted_return:+.1f}% | "
        f"{result.flow} | {names}{extra}"
    )


def chunks(lines: list[str]) -> list[str]:
    output, current = [], ""
    for item in lines:
        addition = item + "\n"
        if current and len(current) + len(addition) > TG_LIMIT:
            output.append(current.rstrip())
            current = addition
        else:
            current += addition
    if current.strip():
        output.append(current.rstrip())
    return output


def messages(results: list) -> list[str]:
    actionable = sorted((item for item in results if item.category == "actionable"), key=rank, reverse=True)[:MAX_ACTIONABLE]
    wait = sorted((item for item in results if item.category == "wait"), key=rank, reverse=True)[:MAX_WAIT]
    risk = sorted((item for item in results if item.category == "risk"), key=rank, reverse=True)[:MAX_RISK]

    if actionable or wait or risk:
        lines = [
            "POLITICAL DISCLOSURE OPPORTUNITIES",
            f"Model: {MODEL_VERSION}",
            f"Analysed: {len(results)} tickers | Shown: {len(actionable) + len(wait) + len(risk)}",
            "C = political disclosure conviction | E = entry quality from the latest completed market session",
            "",
        ]
        for title, items in (
            ("BEST ACTIONABLE", actionable),
            ("HIGH CONVICTION - WAIT FOR ENTRY", wait),
            ("CONFLICTING / HIGHER RISK", risk),
        ):
            if items:
                lines += [title] + [line(item) for item in items] + [""]
        lines += [
            "Role relevance reflects policy-access overlap, not possession of confidential information.",
            "Active capital excludes historical context and discounts late disclosures.",
            "Screening signal only.",
        ]
        return chunks(lines)

    context = sorted((item for item in results if item.category == "context"), key=rank, reverse=True)[:MAX_NEAREST]
    nearest = sorted((item for item in results if item.category == "other" and item.conviction >= 15), key=rank, reverse=True)[:MAX_NEAREST]
    lines = [
        "POLITICAL DISCLOSURE MONITOR",
        f"Model: {MODEL_VERSION}",
        "No new political disclosures met the thresholds.",
        f"Analysed: {len(results)} tickers | Qualified: 0",
        "",
    ]
    if context:
        lines += ["POLITICAL MARKET CONTEXT"] + [line(item) for item in context] + [""]
    if nearest:
        lines += ["NEAREST SIGNALS - NOT QUALIFIED"] + [line(item) for item in nearest] + [""]
    lines += [
        "Late-disclosed events are weighted below fresh transactions.",
        "Repeated disclosures are suppressed by the transaction ledger.",
    ]
    return chunks(lines)


def send(items: list[str]) -> None:
    for index, item in enumerate(items):
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": item, "disable_web_page_preview": True},
            timeout=20,
        )
        response.raise_for_status()
        if index < len(items) - 1:
            time.sleep(1)


def failure(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": f"Political disclosure monitor failure\nModel: {MODEL_VERSION}\n{text}",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Could not send failure alert")


def main() -> int:
    if not TOKEN or not CHAT_ID:
        if _bool_env("POLITICAL_DIGEST_SEND_TELEGRAM", True):
            logger.error("Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID")
            return 1
    if not lock():
        return 0

    try:
        logger.info("Running model %s at %s", MODEL_VERSION, datetime.now(TZ).isoformat())
        run = run_congress_adapter_detailed(min_conviction=15.0)
        scored = run.scan.ticker_results
        if not scored:
            failure("No ticker produced usable price analytics.")
            return 0
        digest_enabled = _bool_env("POLITICAL_DIGEST_ENABLED", True)
        legacy_output = _bool_env("POLITICAL_DIGEST_LEGACY_OUTPUT", False)
        send_telegram = _bool_env("POLITICAL_DIGEST_SEND_TELEGRAM", True)
        preview_path = _digest_preview_path()
        telegram_sent = False
        sent_at = ""

        if digest_enabled and not legacy_output:
            digest_text = render_digest(run.digest_plan, now_sg=datetime.now(TZ))
            write_digest_preview(preview_path, digest_text)
            if digest_text is not None:
                logger.info("Rendered political digest preview to %s", preview_path)
            else:
                logger.info("No political digest rendered for this run.")
            if digest_text and send_telegram:
                output = chunk_digest(digest_text)
                send(output)
                telegram_sent = True
                sent_at = datetime.now(TZ).isoformat()
                logger.info(
                    "Sent %d Telegram digest part(s) | signals=%d | payload=%s",
                    len(output),
                    len(run.signals),
                    run.scan.metadata.payload_sha256,
                )
            elif digest_text:
                logger.info("Digest delivery skipped because POLITICAL_DIGEST_SEND_TELEGRAM=false")
            digest_rows = digest_log_rows(
                run.digest_plan,
                run_id=str(os.getenv("GITHUB_RUN_ID", datetime.now(TZ).isoformat())),
                payload_hash=run.payload_hash,
                telegram_included=telegram_sent,
                telegram_sent_at=sent_at,
            )
            persist_digest_rows(load_political_archive_state(), digest_rows)
            return 0

        output = messages(scored)
        if send_telegram:
            send(output)
            logger.info(
                "Sent %d Telegram message(s) | signals=%d | payload=%s",
                len(output),
                len(run.signals),
                run.scan.metadata.payload_sha256,
            )
        else:
            preview_path.write_text("\n\n".join(output) + "\n", encoding="utf-8")
            logger.info("Legacy output rendered to %s without Telegram delivery", preview_path)
        return 0
    except Exception as exc:
        logger.exception("Unhandled failure: %s", exc)
        if _bool_env("POLITICAL_DIGEST_SEND_TELEGRAM", True):
            failure(str(exc)[:500])
        return 1
    finally:
        unlock()


if __name__ == "__main__":
    raise SystemExit(main())
