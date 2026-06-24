from __future__ import annotations

import json
import os
from typing import Any

import requests


DEFAULT_MODEL = "gpt-4.1-mini"


def build_feroldi_prompt(candidate: dict[str, Any]) -> str:
    return (
        "You are drafting a Feroldi-style fundamental stock review for a "
        "human investor. Use only the supplied data. If data is missing, "
        "say manual review is needed. Return compact JSON with keys: "
        "score, quality_summary, bull_case, bear_case, red_flags, "
        "manual_review_needed, confidence.\n\n"
        f"Ticker: {candidate.get('Ticker', '')}\n"
        f"Company: {candidate.get('Company Name', '')}\n"
        f"BTD Score: {candidate.get('BTD Score', '')}\n"
        f"BTD Summary: {candidate.get('BTD Summary', '')}\n"
        f"Revenue Growth: {candidate.get('Revenue Growth', '')}\n"
        f"Gross Margin: {candidate.get('Gross Margin', '')}\n"
        f"EBITDA Margin: {candidate.get('EBITDA Margin', '')}\n"
        f"Total Revenue: {candidate.get('Total Revenue', '')}\n"
        f"Enterprise Value: {candidate.get('Enterprise Value', '')}\n"
        f"Signal Source: {candidate.get('Source', '')}\n"
        f"Signal Reason: {candidate.get('Discovery Reason', '')}\n"
    )


def _extract_text(response_json: dict[str, Any]) -> str:
    if "output_text" in response_json:
        return str(response_json["output_text"])

    chunks: list[str] = []
    for item in response_json.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                chunks.append(str(content.get("text", "")))
    return "\n".join(chunk for chunk in chunks if chunk)


def request_feroldi_draft(
    candidate: dict[str, Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": build_feroldi_prompt(candidate),
            "text": {"format": {"type": "json_object"}},
        },
        timeout=45,
    )
    response.raise_for_status()

    raw_text = _extract_text(response.json()).strip()
    if not raw_text:
        return None

    return json.loads(raw_text)


def draft_to_candidate_updates(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "AI Feroldi Score": draft.get("score", ""),
        "AI Quality Summary": draft.get("quality_summary", ""),
        "AI Bull Case": draft.get("bull_case", ""),
        "AI Bear Case": draft.get("bear_case", ""),
        "AI Red Flags": draft.get("red_flags", ""),
        "AI Manual Review Needed": draft.get("manual_review_needed", ""),
        "AI Confidence": draft.get("confidence", ""),
    }
