from __future__ import annotations

import re
from typing import Any

from scanners.congress.models import AssetIntent


BROAD_MARKET_ETFS = {
    "SPY",
    "VOO",
    "IVV",
    "VTI",
    "QQQ",
    "DIA",
    "IWM",
}

SECTOR_ETF_HINTS = (
    "select sector",
    "semiconductor",
    "financial select",
    "health care select",
    "energy select",
    "regional bank",
    "biotech",
    "homebuilders",
    "aerospace",
    "defense",
    "cybersecurity",
)

MUTUAL_FUND_TERMS = ("mutual fund", "managed account", "managed portfolio", "trust account")
FIXED_INCOME_TERMS = ("bond", "treasury", "fixed income", "debenture", "note", "cd")
CRYPTO_TERMS = ("bitcoin", "ethereum", "crypto", "cryptocurrency")
NON_DIRECTIONAL_TERMS = ("dividend reinvest", "automatic", "vesting", "grant", "transfer", "donation", "conversion")


def _text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in (
            "ticker",
            "asset_name",
            "asset_type",
            "description",
            "asset_description",
            "transaction_type",
            "comments",
            "comment",
        )
    ).lower()


def classify_asset_intent(item: dict[str, Any]) -> AssetIntent:
    ticker = str(item.get("ticker") or "").strip().upper()
    text = _text(item)
    tx_type = str(item.get("transaction_type") or item.get("type") or "").lower()
    option_side = str(item.get("option_type") or item.get("put_call") or item.get("call_put") or "").strip().lower()
    if option_side not in {"call", "put"}:
        match = re.search(r"\b(call|put)\b", text)
        option_side = match.group(1) if match else ""

    if any(term in text for term in NON_DIRECTIONAL_TERMS):
        return AssetIntent("NON_DIRECTIONAL", False, False, False, True, False, 0.0, "non_discretionary")
    if any(term in text for term in MUTUAL_FUND_TERMS):
        return AssetIntent("MUTUAL_FUND", False, False, False, True, False, 0.0, "managed_or_fund")
    if any(term in text for term in FIXED_INCOME_TERMS):
        return AssetIntent("BOND_OR_FIXED_INCOME", False, False, False, True, False, 0.0, "fixed_income")
    if any(term in text for term in CRYPTO_TERMS):
        return AssetIntent("CRYPTO", False, False, False, False, False, 0.0, "crypto")

    if option_side == "put":
        return AssetIntent("PUT_PURCHASE", True, False, True, False, False, 0.0, "bearish_put")
    if option_side == "call":
        if "exercise" in tx_type or "exercise" in text:
            return AssetIntent("CALL_EXERCISE", True, False, False, False, False, 12.0, "call_exercise")
        if "sale" in tx_type or "sell" in tx_type:
            return AssetIntent("CALL_SALE", True, False, False, False, False, 0.0, "call_sale")
        return AssetIntent("NEW_CALL_PURCHASE", True, True, False, False, False, 23.0, "bullish_call")

    if ticker in BROAD_MARKET_ETFS:
        return AssetIntent("BROAD_MARKET_ETF", False, True, False, False, True, 2.0, "broad_market")
    if "etf" in text or "exchange traded fund" in text:
        if any(term in text for term in SECTOR_ETF_HINTS):
            return AssetIntent("SECTOR_ETF", False, True, False, False, False, 10.0, "sector_etf")
        return AssetIntent("OTHER_ETF", False, True, False, False, False, 6.0, "other_etf")
    if "common stock" in text or "ordinary share" in text or "stock" in text:
        return AssetIntent("INDIVIDUAL_STOCK", True, True, False, False, False, 25.0, "stock")
    return AssetIntent("UNRESOLVED", False, False, False, False, False, 0.0, "unresolved")

