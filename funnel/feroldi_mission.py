from __future__ import annotations

"""M03 — Mission statement quality analysis (deterministic, no LLM)."""

import re

from funnel.feroldi_config import (
    ACTION_VERBS,
    FINANCE_ONLY_PHRASES,
    MISSION_MAX_COMMAS,
    MISSION_MAX_PARENS,
    MISSION_MAX_WORDS,
    MISSION_MIN_WORDS,
    PURPOSE_OUTCOMES,
    VAGUE_TERMS,
)


def _word_count(text: str) -> int:
    return len(text.split())


def _sentence_count(text: str) -> int:
    return len([s for s in re.split(r"[.!?]+", text) if s.strip()])


def _count_punctuation(text: str, chars: str) -> int:
    return sum(1 for ch in text if ch in chars)


def _contains_any_word(text: str, word_set: frozenset) -> bool:
    """Check if any word from the set appears as a whole word in text."""
    lower = text.lower()
    for word in word_set:
        # Multi-word phrases
        if " " in word:
            if word in lower:
                return True
        else:
            # Single-word match with word boundaries
            if re.search(r"\b" + re.escape(word) + r"\b", lower):
                return True
    return False


def _contains_phrase(text: str, phrases: frozenset) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in phrases)


def _has_undefined_acronym(text: str) -> int:
    """Count uppercase words (2+ chars) that may be undefined acronyms."""
    words = text.split()
    count = 0
    for word in words:
        clean = word.strip("(),;:.\"'")
        if len(clean) >= 2 and clean.isupper() and clean.isalpha():
            count += 1
    return count


def analyse_mission(text: str) -> dict:
    """Deterministically analyse a mission statement text.

    Returns a dict with all M03 sub-scores and flags.
    No LLM, no semantic analysis, no inference.
    """
    words = _word_count(text)
    sentences = _sentence_count(text)
    punctuation = _count_punctuation(text, ",:;")
    parens = _count_punctuation(text, "()") // 2

    # Simple point
    simple_point = 0
    if (sentences <= 1
            and MISSION_MIN_WORDS <= words <= MISSION_MAX_WORDS
            and punctuation <= MISSION_MAX_COMMAS
            and parens <= MISSION_MAX_PARENS):
        simple_point = 1

    # Clear point
    action_verb_found = _contains_any_word(text, ACTION_VERBS)
    # Object/offering: the text mentions a product, service, or platform
    # (heuristic: sentence has a noun after the verb)
    object_found = words >= 5  # minimum structure suggests object exists
    beneficiary_found = _contains_any_word(text, PURPOSE_OUTCOMES)
    outcome_words = {"customer", "user", "people", "client", "patient", "consumer",
                     "student", "business", "enterprise", "developer", "community",
                     "family", "worker", "employee", "partner"}
    beneficiary_found = beneficiary_found or _contains_any_word(text, frozenset(outcome_words))
    undefined_acronym_count = _has_undefined_acronym(text)
    vague_term_count = sum(1 for _ in re.finditer(
        r"\b(?:" + "|".join(re.escape(t) for t in VAGUE_TERMS) + r")\b",
        text.lower(),
    ))
    clear_point = 0
    if (action_verb_found
            and object_found
            and beneficiary_found
            and undefined_acronym_count == 0
            and vague_term_count <= 2):  # Allow up to 2 vague terms
        clear_point = 1

    # Inspirational point
    outcome_found = _contains_any_word(text, PURPOSE_OUTCOMES)
    financial_only_flag = _contains_phrase(text, FINANCE_ONLY_PHRASES)
    inspirational_point = 0
    if outcome_found and not financial_only_flag:
        inspirational_point = 1

    return {
        "word_count": words,
        "sentence_count": sentences,
        "punctuation_count": punctuation,
        "parenthetical_count": parens,
        "action_verb_found": action_verb_found,
        "object_found": object_found,
        "beneficiary_found": beneficiary_found,
        "outcome_found": outcome_found,
        "undefined_acronym_count": undefined_acronym_count,
        "vague_term_count": vague_term_count,
        "financial_only_flag": financial_only_flag,
        "simple_point": simple_point,
        "clear_point": clear_point,
        "inspirational_point": inspirational_point,
    }
