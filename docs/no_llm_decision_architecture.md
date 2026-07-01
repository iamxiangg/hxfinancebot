# No-LLM Decision Architecture

## Principle

All investment decision values in `hxfinancebot` must be derived from deterministic sources:

1. Structured public API fields
2. SEC XBRL facts
3. Deterministic HTML, XML, JSON, or table parsing
4. Transparent mathematical formulas
5. Explicit configuration rules
6. Manual human-entered inputs with source evidence

No LLM (OpenAI, Anthropic, Gemini, local models, embeddings, etc.) may derive, infer, estimate, classify, or score any value used in Feroldi scoring, financing risk, dilution, thesis direction, activist classification, clinical-trial interpretation, government-demand metrics, candidate admission, portfolio displacement, position sizing, Telegram eligibility, or buy/add/hold/reduce/reject recommendations.

## Environment Variable

```
NO_LLM_DECISIONS=true  # default
```

When `NO_LLM_DECISIONS=true`:
- Decision-critical modules refuse to run without it
- `feroldi_ai` must not be imported by decision workflows
- AI-generated fields (AI Feroldi Score, AI Quality Summary, etc.) are stripped from decision paths
- All deterministic workflows run successfully with no `OPENAI_API_KEY`

## Runtime Guardrails

### `scanners/no_llm_guard.py`

| Function | Purpose |
|---|---|
| `no_llm_decisions()` | Returns `True` when LLM decisions are disabled |
| `require_no_llm()` | Raises `RuntimeError` if LLM decisions are enabled in a decision-critical module |
| `is_known_llm_endpoint(url)` | Detects calls to known LLM provider endpoints |
| `is_ai_field(name)` | Detects AI-generated fields |
| `strip_ai_fields(candidate)` | Removes all AI fields from a candidate record |
| `raise_if_feroldi_ai_imported()` | Runtime invariant: raises if `feroldi_ai` is imported |
| `check_production_safeguards()` | Returns list of production warnings |

### LLM Endpoint Blocklist

The following domains are detected as LLM endpoints:

```
api.openai.com, api.anthropic.com, generativelanguage.googleapis.com,
api.gemini.google.com, api.together.xyz, api.mistral.ai,
api.perplexity.ai, api.cohere.ai, api.deepseek.com, api.x.ai,
api.groq.com, openrouter.ai, llmfoundry.studio
```

### AI Fields (Never Influence Decisions)

```
AI Feroldi Score, AI Quality Summary, AI Bull Case, AI Bear Case,
AI Red Flags, AI Manual Review Needed, AI Confidence, AI Last Updated
```

## When a Value Cannot Be Derived

Store:

```text
Status = MANUAL_REQUIRED
Value = blank
Reason = explicit reason
```

Never convert missing information into zero. Never guess from narrative text.

## Provenance Tracking

Every derived field must retain:

```
value, unit, as_of, source, source_record_id, source_url,
source_field, formula_or_rule, derivation_type, confidence_status,
observed_at, payload_hash
```

`derivation_type` must be one of: `API_FIELD`, `XBRL_FACT`, `DETERMINISTIC_PARSE`, `FORMULA`, `RULE`, `MANUAL`, `UNAVAILABLE`.

## Testing

All unit tests must:
1. Run with `NO_LLM_DECISIONS=true` (no `OPENAI_API_KEY`)
2. Not call any known LLM endpoint
3. Not import `feroldi_ai` in decision-critical modules
4. Not use AI fields in gate or recommendation logic
