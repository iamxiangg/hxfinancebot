# Entity Master

## Overview

The Entity Master provides a canonical entity layer for all scanners and decision engines. CIK (SEC Central Index Key) is the primary issuer identity — ticker alone is never used as a permanent identity.

## Sheet

```
Entity_Master
```

## Fields

| Field | Source | Automation | Notes |
|---|---|---|---|
| Entity ID | Derived from CIK | Automated | Format: `CIK{10-digit}` |
| Ticker | SEC submissions | Automated | Current trading symbol |
| Exchange | SEC submissions | Automated | e.g., NASDAQ, NYSE |
| Security Type | Manual/override | Manual | e.g., common_stock, preferred |
| Active? | Manual | Manual | FALSE for delisted/acquired |
| CIK | SEC company_tickers.json | Automated | 10-digit zero-padded |
| SIC | SEC submissions | Automated | Standard Industrial Classification |
| SIC Description | SEC submissions | Automated | |
| Current Legal Name | SEC company_tickers.json | Automated | |
| Former Legal Names | SEC submissions | Automated | From `formerCompanyNames` |
| Former Tickers | SEC submissions | Automated | From `tickers` array |
| Parent Entity ID | Exhibit 21 / manual | Manual | |
| Subsidiary Legal Names | Exhibit 21 / manual | Manual | |
| Government Recipient Names | USAspending / manual | Manual | |
| Government UEIs | USAspending / manual | Manual | |
| Clinical Trial Sponsor Names | ClinicalTrials.gov / manual | Manual | |
| Yahoo Ticker | Manual | Manual | May differ from SEC ticker |
| Mapping Status | Engine | Automated | EXACT, FUZZY_SUGGESTED, MANUAL_REQUIRED |
| Mapping Confidence | Engine | Automated | HIGH, MEDIUM, LOW, SUGGESTED_REVIEW |
| Evidence URL | SEC provider | Automated | |
| Last Verified | Engine | Automated | ISO timestamp |
| Manual Override? | Manual | Manual | TRUE if manually edited |

## Architecture

```
scanners/entity_master/engine.py  — Deterministic engine
models/common.py                  — Shared dataclasses (EntityMapping, etc.)
providers/sec/                    — SEC data provider (existing)
```

## Engine API

### `EntityMasterEngine`

```python
engine = EntityMasterEngine(sec_provider=provider)

# Resolve a single ticker
mapping = engine.resolve_entity("AAPL")
# Returns EntityMapping with CIK, names, exchange, etc.

# Batch resolve
results = engine.resolve_batch(["AAPL", "MSFT", "GOOGL"])

# Fuzzy suggestion (never auto-applied)
suggestion = engine.suggest_fuzzy_mapping(
    ticker="AAPL",
    candidate_name="Apple Computer Inc.",
    similarity_score=0.92,
)
# suggestion.status == "MANUAL_REQUIRED"  # Always!
```

## Mapping Rules

### Automatic (EXACT)
Only when based on exact identifiers:
- CIK match
- Exact legal name
- Explicit parent-subsidiary identifier
- Exact historical ticker/name record

### Fuzzy (MANUAL_REQUIRED)
Generated only as review suggestions:
- Partial name match
- Similar ticker
- Never activated automatically

## Environment Variables

```
ENTITY_MASTER_ENABLE=true                       # Enable entity master
ENTITY_MASTER_FUZZY_MIN_CONFIDENCE=0.85          # Minimum fuzzy match confidence
ENTITY_MASTER_FUZZY_MANUAL_ONLY=true             # All fuzzy matches require manual approval
```

## Refresh Cadence

Weekly entity refresh is recommended. Entity mappings are stable over short periods.

## Limitations

- Subsidiary extraction from Exhibit 21 requires manual review
- Parent-subsidiary relationships are manual
- Government UEI and clinical trial sponsor mapping is manual
- Name similarity is never treated as ownership
