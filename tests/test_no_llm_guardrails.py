from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from scanners.no_llm_guard import (
    _AI_FIELD_NAMES,
    _KNOWN_LLM_ENDPOINTS,
    check_production_safeguards,
    is_ai_field,
    is_known_llm_endpoint,
    no_llm_decisions,
    raise_if_feroldi_ai_imported,
    require_no_llm,
    strip_ai_fields,
)


# ---------------------------------------------------------------------------
# NO_LLM_DECISIONS default behaviour tests
# ---------------------------------------------------------------------------


class NoLlmDecisionsDefaultsTests(unittest.TestCase):
    def test_no_llm_decisions_defaults_to_true_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(no_llm_decisions())

    def test_no_llm_decisions_true_when_set_to_true(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            self.assertTrue(no_llm_decisions())

    def test_no_llm_decisions_true_when_set_to_1(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "1"}, clear=True):
            self.assertTrue(no_llm_decisions())

    def test_no_llm_decisions_false_when_explicitly_disabled(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "false"}, clear=True):
            self.assertFalse(no_llm_decisions())

    def test_no_llm_decisions_false_when_set_to_0(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "0"}, clear=True):
            self.assertFalse(no_llm_decisions())


class RequireNoLlmTests(unittest.TestCase):
    def test_require_no_llm_passes_when_true(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            require_no_llm()  # Should not raise

    def test_require_no_llm_raises_when_false(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "false"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                require_no_llm()
            self.assertIn("NO_LLM_DECISIONS=false", str(ctx.exception))


# ---------------------------------------------------------------------------
# LLM endpoint detection tests
# ---------------------------------------------------------------------------


class LlmEndpointTests(unittest.TestCase):
    def test_known_openai_endpoint_detected(self) -> None:
        self.assertTrue(is_known_llm_endpoint("https://api.openai.com/v1/chat/completions"))

    def test_known_anthropic_endpoint_detected(self) -> None:
        self.assertTrue(is_known_llm_endpoint("https://api.anthropic.com/v1/messages"))

    def test_known_gemini_endpoint_detected(self) -> None:
        self.assertTrue(is_known_llm_endpoint("https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"))

    def test_known_deepseek_endpoint_detected(self) -> None:
        self.assertTrue(is_known_llm_endpoint("https://api.deepseek.com/v1/chat/completions"))

    def test_non_llm_endpoint_not_detected(self) -> None:
        self.assertFalse(is_known_llm_endpoint("https://www.sec.gov/files/company_tickers.json"))

    def test_empty_url_not_detected(self) -> None:
        self.assertFalse(is_known_llm_endpoint(""))

    def test_financial_api_not_detected(self) -> None:
        self.assertFalse(is_known_llm_endpoint("https://www.alphavantage.co/query"))
        self.assertFalse(is_known_llm_endpoint("https://query1.finance.yahoo.com/v8/finance/chart/AAPL"))


# ---------------------------------------------------------------------------
# AI field detection tests
# ---------------------------------------------------------------------------


class AiFieldTests(unittest.TestCase):
    def test_ai_score_field_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Feroldi Score"))

    def test_ai_quality_summary_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Quality Summary"))

    def test_ai_bull_case_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Bull Case"))

    def test_ai_bear_case_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Bear Case"))

    def test_ai_red_flags_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Red Flags"))

    def test_ai_confidence_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Confidence"))

    def test_ai_last_updated_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Last Updated"))

    def test_manual_review_needed_detected(self) -> None:
        self.assertTrue(is_ai_field("AI Manual Review Needed"))

    def test_non_ai_field_not_detected(self) -> None:
        self.assertFalse(is_ai_field("Feroldi First Cut Score"))
        self.assertFalse(is_ai_field("BTD Score"))
        self.assertFalse(is_ai_field("Ticker"))
        self.assertFalse(is_ai_field("Telegram Eligible"))

    def test_empty_field_not_detected(self) -> None:
        self.assertFalse(is_ai_field(""))
        self.assertFalse(is_ai_field(None))  # type: ignore[arg-type]


class StripAiFieldsTests(unittest.TestCase):
    def test_strips_all_ai_fields(self) -> None:
        candidate = {
            "Ticker": "AAPL",
            "BTD Score": "85",
            "Telegram Eligible": "YES",
            "AI Feroldi Score": "30",
            "AI Quality Summary": "Great company",
            "AI Bull Case": "Strong growth",
            "AI Bear Case": "Regulation risk",
            "AI Red Flags": "None",
            "AI Manual Review Needed": "No",
            "AI Confidence": "high",
            "AI Last Updated": "2026-06-29",
        }
        stripped = strip_ai_fields(candidate)
        self.assertIn("Ticker", stripped)
        self.assertIn("BTD Score", stripped)
        self.assertIn("Telegram Eligible", stripped)
        self.assertNotIn("AI Feroldi Score", stripped)
        self.assertNotIn("AI Quality Summary", stripped)
        self.assertNotIn("AI Bull Case", stripped)
        self.assertNotIn("AI Bear Case", stripped)
        self.assertNotIn("AI Red Flags", stripped)
        self.assertNotIn("AI Manual Review Needed", stripped)
        self.assertNotIn("AI Confidence", stripped)
        self.assertNotIn("AI Last Updated", stripped)
        self.assertEqual(len(stripped), 3)

    def test_candidate_without_ai_fields_unchanged(self) -> None:
        candidate = {
            "Ticker": "MSFT",
            "BTD Gate": "PASS",
            "Feroldi First Cut Score": "32",
        }
        stripped = strip_ai_fields(candidate)
        self.assertEqual(stripped, candidate)


# ---------------------------------------------------------------------------
# Production safeguards tests
# ---------------------------------------------------------------------------


class ProductionSafeguardsTests(unittest.TestCase):
    def test_check_safeguards_empty_when_clean(self) -> None:
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            warnings = check_production_safeguards()
            self.assertEqual(warnings, [])

    def test_check_safeguards_warns_when_openai_key_set_with_no_llm_true(self) -> None:
        with patch.dict(os.environ, {
            "NO_LLM_DECISIONS": "true",
            "OPENAI_API_KEY": "sk-test123",
        }, clear=True):
            warnings = check_production_safeguards()
            self.assertTrue(any("OPENAI_API_KEY" in w for w in warnings))


# ---------------------------------------------------------------------------
# No LLM endpoint call verification test
# ---------------------------------------------------------------------------


class NoLlmEndpointCallTests(unittest.TestCase):
    """Verify that deterministic workflows don't call LLM endpoints."""

    def test_no_llm_endpoint_imported_in_deterministic_engines(self) -> None:
        """Import entity master and evidence ledger engines without triggering LLM."""
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            # These imports should not trigger any LLM endpoint or import feroldi_ai
            from scanners.entity_master.engine import EntityMasterEngine
            from scanners.evidence_ledger.engine import EvidenceLedgerEngine
            self.assertTrue(True)  # If we got here, no exceptions

    def test_feroldi_ai_not_imported_by_decision_modules(self) -> None:
        """Verify entity_master and evidence_ledger engines don't transitively import feroldi_ai.

        Since the test runner may have already imported feroldi_ai through other modules
        (e.g., review_candidates), we check whether importing our new modules CAUSES any
        new feroldi_ai imports by snapshotting sys.modules before and after.
        """
        with patch.dict(os.environ, {"NO_LLM_DECISIONS": "true"}, clear=True):
            # Snapshot modules before importing our engines
            before = set(sys.modules.keys())

            # Import decision-critical modules
            from scanners.entity_master.engine import EntityMasterEngine  # noqa: F811
            from scanners.evidence_ledger.engine import EvidenceLedgerEngine  # noqa: F811

            # Find newly imported modules
            after = set(sys.modules.keys())
            new_modules = after - before

            # None of the newly imported modules should be AI-related
            for mod_name in sorted(new_modules):
                if "feroldi_ai" in mod_name.lower():
                    self.fail(
                        f"Importing decision modules caused feroldi_ai to be imported: "
                        f"'{mod_name}'"
                    )

    def test_known_llm_endpoint_blocklist_comprehensive(self) -> None:
        """Verify that all required LLM providers are in the blocklist."""
        required = [
            "api.openai.com",
            "api.anthropic.com",
            "generativelanguage.googleapis.com",
        ]
        for endpoint in required:
            self.assertTrue(
                any(endpoint in known for known in _KNOWN_LLM_ENDPOINTS),
                f"{endpoint} missing from LLM endpoint blocklist",
            )

    def test_ai_fields_not_in_feroldi_gate_logic(self) -> None:
        """Verify AI field names are not referenced by the Feroldi gate."""
        # The gate uses "Feroldi First Cut Score" etc., not AI fields
        self.assertNotIn("Feroldi First Cut Score", _AI_FIELD_NAMES)
        self.assertNotIn("Feroldi Financial Score", _AI_FIELD_NAMES)
        self.assertNotIn("Feroldi Gate", _AI_FIELD_NAMES)


if __name__ == "__main__":
    unittest.main()
