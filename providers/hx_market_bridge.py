from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

import requests


class HxMarketBridgeError(RuntimeError):
    pass


def _config() -> tuple[str, str]:
    url = os.environ.get("HX_MARKET_INGEST_URL", "").strip()
    secret = os.environ.get("HX_MARKET_INGEST_SECRET", "").strip()
    if not url:
        raise HxMarketBridgeError("HX_MARKET_INGEST_URL is not configured")
    if not secret:
        raise HxMarketBridgeError("HX_MARKET_INGEST_SECRET is not configured")
    return url, secret


def post_bridge(payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    url, secret = _config()
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-hx-timestamp": timestamp,
            "x-hx-signature": f"sha256={signature}",
        },
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise HxMarketBridgeError(
            f"HX market bridge returned HTTP {response.status_code} with non-JSON body"
        ) from exc
    if not response.ok:
        raise HxMarketBridgeError(
            f"HX market bridge returned HTTP {response.status_code}: {data}"
        )
    if not isinstance(data, dict):
        raise HxMarketBridgeError("HX market bridge response must be a JSON object")
    return data


def ping() -> dict[str, Any]:
    return post_bridge({"action": "PING"})


def get_eligible_universe() -> list[dict[str, Any]]:
    response = post_bridge({"action": "GET_ELIGIBLE_UNIVERSE"})
    items = response.get("items", [])
    if not isinstance(items, list):
        raise HxMarketBridgeError("Eligible-universe response did not contain an items array")
    return [item for item in items if isinstance(item, dict)]


def get_capability_work(*, limit: int = 25) -> list[dict[str, Any]]:
    response = post_bridge({"action": "GET_CAPABILITY_WORK", "limit": int(limit)})
    items = response.get("items", [])
    if not isinstance(items, list):
        raise HxMarketBridgeError("Capability-work response did not contain an items array")
    return [item for item in items if isinstance(item, dict)]


def ingest_capability_audit(
    results: list[dict[str, Any]],
    *,
    source_reference: str,
) -> dict[str, Any]:
    return post_bridge(
        {
            "action": "INGEST_CAPABILITY_AUDIT",
            "results": results,
            "source_reference": source_reference,
        },
        timeout=60,
    )


def ingest_market_structure_snapshot(
    *,
    run: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return post_bridge(
        {
            "action": "INGEST_MARKET_STRUCTURE_SNAPSHOT",
            "run": run,
            "items": items,
        },
        timeout=90,
    )


def ingest_market_reaction_states(
    *,
    market_snapshot_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return post_bridge(
        {
            "action": "INGEST_MARKET_REACTION_STATES",
            "market_snapshot_id": market_snapshot_id,
            "items": items,
        },
        timeout=90,
    )
