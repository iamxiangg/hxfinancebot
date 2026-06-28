from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

from providers.sec.base import SECProvider
from providers.sec import get_sec_provider
from scanners.fundamental_inflection.financial_series import build_financial_series
from scanners.fundamental_inflection.models import (
    BUSINESS_MODEL_EXCLUDE_SIC,
    BUSINESS_MODEL_EXCLUDE_TOKENS,
    FundamentalInflectionConfig,
    InflectionResult,
    MODEL_VERSION,
)
from scanners.fundamental_inflection.scoring import (
    build_result,
    evaluate_balance_sheet,
    evaluate_cash_flow,
    evaluate_gross_economics,
    evaluate_operating_leverage,
    evaluate_per_share,
    evaluate_revenue_growth,
    evaluate_working_capital,
    score_and_classify,
)


logger = logging.getLogger(__name__)


def _normalise_ticker(value: str) -> str:
    return str(value or "").strip().upper().replace(".", "-")


def _is_excluded_business_model(
    name: str,
    sic: str,
) -> bool:
    sic_clean = str(sic or "").strip()
    if sic_clean[:2] in BUSINESS_MODEL_EXCLUDE_SIC:
        return True
    name_lower = f" {str(name or '').strip().lower()} "
    if any(token in name_lower for token in BUSINESS_MODEL_EXCLUDE_TOKENS):
        return True
    return False


def run_inflection_scan(
    *,
    config: FundamentalInflectionConfig | None = None,
    sec_provider: SECProvider | None = None,
    test_tickers: list[str] | None = None,
    observed_at: str | None = None,
) -> list[InflectionResult]:
    config = config or FundamentalInflectionConfig.from_env()
    if not config.enable:
        return []

    sec_provider = sec_provider or get_sec_provider()
    observed = datetime.fromisoformat(
        (observed_at or datetime.now(UTC).replace(microsecond=0).isoformat()).replace("Z", "+00:00")
    )

    tickers: list[str] = []
    if test_tickers:
        tickers = [_normalise_ticker(t) for t in test_tickers]
    else:
        env_tickers = str(os.getenv("FUNDAMENTAL_INFLECTION_TICKERS", "")).strip()
        if env_tickers:
            tickers = [_normalise_ticker(p) for p in env_tickers.split(",") if _normalise_ticker(p)]
        else:
            logger.warning("No tickers configured for fundamental inflection scan")
            return []

    if not tickers:
        return []

    results: list[InflectionResult] = []
    seen_accessions: set[str] = set()

    for ticker in tickers:
        try:
            profile = sec_provider.company_profile(ticker)
        except Exception as exc:
            logger.warning("Fundamental inflection ticker lookup failed for %s: %s", ticker, exc.__class__.__name__)
            continue

        if _is_excluded_business_model(profile.name, profile.sic):
            continue

        try:
            series = build_financial_series(
                sec_provider,
                ticker,
                as_of=observed,
                min_quarters=config.min_quarters,
            )
        except Exception as exc:
            logger.warning("Fundamental inflection series failed for %s: %s", ticker, exc.__class__.__name__)
            continue

        if series.data_confidence == "low" and series.errors:
            continue

        usable = series.usable_quarters
        if len(usable) < config.min_quarters:
            continue

        latest_q = usable[-1]
        if latest_q.accession in seen_accessions:
            continue
        seen_accessions.add(latest_q.accession)

        revenue = evaluate_revenue_growth(usable, config)
        if revenue.yoy_growth < config.min_revenue_growth:
            continue

        filing_date_str = latest_q.filed_at.isoformat() if latest_q.filed_at else None

        gross = evaluate_gross_economics(usable, revenue)
        operating = evaluate_operating_leverage(usable, revenue)
        cash_flow = evaluate_cash_flow(usable)
        per_share = evaluate_per_share(usable, revenue)
        balance = evaluate_balance_sheet(usable, cash_flow, config)
        working_cap = evaluate_working_capital(usable, revenue)

        classification, total_score, risk_flags, economic_conf, extra = score_and_classify(
            revenue, gross, operating, cash_flow, per_share, balance, working_cap, config,
        )

        positive_pillars: list[str] = []
        if revenue.yoy_growth >= config.min_revenue_growth:
            positive_pillars.append("growth")
        if gross.gross_confirmation == "POSITIVE":
            positive_pillars.append("gross_economics")
        if operating.operating_confirmation in ("POSITIVE", "WEAK_POSITIVE"):
            positive_pillars.append("operating_leverage")
        if cash_flow.cash_confirmation in ("POSITIVE", "WEAK_POSITIVE"):
            positive_pillars.append("cash_flow")
        if per_share.per_share_confirmation == "POSITIVE":
            positive_pillars.append("per_share")
        if False:
            positive_pillars.append("business_kpi")

        result = build_result(
            ticker=ticker,
            classification=classification,
            total_score=total_score,
            revenue=revenue,
            gross=gross,
            operating=operating,
            cash_flow=cash_flow,
            per_share=per_share,
            balance=balance,
            working_cap=working_cap,
            positive_pillars=positive_pillars,
            economic_confirmation=economic_conf,
            risk_flags=risk_flags,
            accession=latest_q.accession,
            filing_date=filing_date_str,
            data_confidence=series.data_confidence,
            config=config,
        )

        result.reason = extra.get("reason", "")
        result.details = {
            "model_version": MODEL_VERSION,
            "latest_filing_accession": latest_q.accession,
            "filing_date": filing_date_str,
            "quarter_label": latest_q.quarter_label,
            "gross_margin_latest": gross.gross_margin_latest,
            "gross_margin_prior": gross.gross_margin_prior,
            "operating_margin_latest": operating.operating_margin_latest,
            "operating_margin_prior": operating.operating_margin_prior,
            "incremental_operating_margin": operating.incremental_operating_margin,
            "ttm_fcf_margin": cash_flow.ttm_fcf_margin,
            "prior_ttm_fcf_margin": cash_flow.prior_ttm_fcf_margin,
            "ttm_fcf_margin_change_bps": cash_flow.ttm_fcf_margin_change_bps,
            "fcf_classification": cash_flow.fcf_classification,
            "dilution_classification": per_share.dilution_classification,
            "diluted_share_growth": per_share.diluted_share_growth,
            "revenue_per_share_growth": per_share.revenue_per_share_growth,
            "sbc_to_revenue": per_share.sbc_to_revenue,
            "balance_sheet_classification": balance.balance_sheet_classification,
            "cash": balance.cash,
            "net_cash": balance.net_cash,
            "total_debt": balance.total_debt,
            "cash_runway_months": balance.cash_runway_months,
            "cash_confirmation": cash_flow.cash_confirmation,
            "growth_trend": revenue.trend,
            "growth_consistency": revenue.growth_consistency,
            "quarters_above_20pct": revenue.quarters_above_20pct,
            "ar_divergence": working_cap.ar_divergence,
            "inventory_divergence": working_cap.inventory_divergence,
            "positive_pillars": positive_pillars,
            "pillar_count": len(positive_pillars),
            "economic_confirmation": economic_conf,
            **extra,
        }

        results.append(result)

    results.sort(key=lambda r: (r.total_score, r.pilllar_count), reverse=True)
    return results[: config.max_results]
