from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default)).strip()


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = _env_str(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class RegulatoryMonitorConfig:
    enabled: bool = True
    sources: list[str] = field(default_factory=lambda: ["clinicaltrials", "sec", "drugs_at_fda", "openfda", "configured_ir"])
    state_backend: str = "auto"
    state_dir: str = "funnel_output/regulatory_state"
    audit_dir: str = "funnel_output/regulatory_audit"
    bootstrap_lookback_days: int = 30
    bootstrap_suppress_notifications: bool = True
    incremental_overlap_days: int = 3
    send_telegram: bool = True
    max_detailed_events: int = 5
    hard_max_detailed_events: int = 10
    max_watchlist_items: int = 20
    standard_retention_trading_days: int = 5
    urgent_retention_trading_days: int = 14
    risk_retention_trading_days: int = 10
    ct_gov_page_size: int = 50
    sec_sic_allowlist: list[str] = field(default_factory=lambda: ["2833", "2834", "2835", "2836", "3841", "3842", "3845", "8731"])
    market_snapshots_enabled: bool = True
    valuation_enabled: bool = True
    configured_sources_path: str = "config/regulatory_sources.json"
    max_issuer_feed_records: int = 50
    ct_gov_api_url: str = "https://clinicaltrials.gov/data-api/api/query/studies"

    @classmethod
    def from_env(cls) -> "RegulatoryMonitorConfig":
        return cls(
            enabled=_env_bool("REGULATORY_MONITOR_ENABLED", True),
            sources=_env_list("REGULATORY_SOURCES", ["clinicaltrials", "sec", "drugs_at_fda", "openfda", "configured_ir"]),
            state_backend=_env_str("REGULATORY_STATE_BACKEND", "auto").lower(),
            state_dir=_env_str("REGULATORY_STATE_DIR", "funnel_output/regulatory_state"),
            audit_dir=_env_str("REGULATORY_AUDIT_DIR", "funnel_output/regulatory_audit"),
            bootstrap_lookback_days=_env_int("REGULATORY_BOOTSTRAP_LOOKBACK_DAYS", 30),
            bootstrap_suppress_notifications=_env_bool("REGULATORY_BOOTSTRAP_SUPPRESS_NOTIFICATIONS", True),
            incremental_overlap_days=_env_int("REGULATORY_INCREMENTAL_OVERLAP_DAYS", 3),
            send_telegram=_env_bool("REGULATORY_SEND_TELEGRAM", True),
            max_detailed_events=_env_int("REGULATORY_MAX_DETAILED_EVENTS", 5),
            hard_max_detailed_events=_env_int("REGULATORY_HARD_MAX_DETAILED_EVENTS", 10),
            max_watchlist_items=_env_int("REGULATORY_MAX_WATCHLIST_ITEMS", 20),
            standard_retention_trading_days=_env_int("REGULATORY_STANDARD_RETENTION_TRADING_DAYS", 5),
            urgent_retention_trading_days=_env_int("REGULATORY_URGENT_RETENTION_TRADING_DAYS", 14),
            risk_retention_trading_days=_env_int("REGULATORY_RISK_RETENTION_TRADING_DAYS", 10),
            ct_gov_page_size=_env_int("REGULATORY_CT_GOV_PAGE_SIZE", 50),
            sec_sic_allowlist=_env_list("REGULATORY_SEC_SIC_ALLOWLIST", ["2833", "2834", "2835", "2836", "3841", "3842", "3845", "8731"]),
            market_snapshots_enabled=_env_bool("REGULATORY_MARKET_SNAPSHOTS_ENABLED", True),
            valuation_enabled=_env_bool("REGULATORY_VALUATION_ENABLED", True),
            ct_gov_api_url=_env_str("REGULATORY_CT_GOV_API_URL", "https://clinicaltrials.gov/data-api/api/query/studies"),
        )

    def configured_sources_payload(self) -> dict:
        path = Path(self.configured_sources_path)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

