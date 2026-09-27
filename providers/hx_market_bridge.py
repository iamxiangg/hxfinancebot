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


_RETRYABLE_ACTIONS = {
    "PING",
    "GET_ELIGIBLE_UNIVERSE",
    "GET_CAPABILITY_WORK",
    "INGEST_CAPABILITY_AUDIT",
    "INGEST_MARKET_STRUCTURE_SNAPSHOT",
    "INGEST_MARKET_REACTION_STATES",
}
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _config() -> tuple[str, str]:
    url = os.environ.get("HX_MARKET_INGEST_URL", "").strip()
    secret = os.environ.get("HX_MARKET_INGEST_SECRET", "").strip()
    if not url:
        raise HxMarketBridgeError("HX_MARKET_INGEST_URL is not configured")
    if not secret:
        raise HxMarketBridgeError("HX_MARKET_INGEST_SECRET is not configured")
    return url, secret


def _retry_config(action: str) -> tuple[int, float]:
    if action not in _RETRYABLE_ACTIONS:
        return 1, 0.0

    try:
        attempts = int(os.environ.get("HX_MARKET_BRIDGE_MAX_ATTEMPTS", "3"))
    except ValueError:
        attempts = 3
    attempts = max(1, min(5, attempts))

    try:
        base_delay = float(os.environ.get("HX_MARKET_BRIDGE_BACKOFF_SECONDS", "1.0"))
    except ValueError:
        base_delay = 1.0
    base_delay = max(0.0, min(10.0, base_delay))
    return attempts, base_delay


def _signed_headers(secret: str, body: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "content-type": "application/json",
        "x-hx-timestamp": timestamp,
        "x-hx-signature": f"sha256={signature}",
    }


def post_bridge(payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    url, secret = _config()
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
    action = str(payload.get("action") or "").upper()
    max_attempts, base_delay = _retry_config(action)

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url,
                data=body.encode("utf-8"),
                headers=_signed_headers(secret, body),
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= max_attempts:
                raise HxMarketBridgeError(
                    f"HX market bridge transport failure action={action or '<unknown>'} "
                    f"after {attempt} attempt(s): {exc.__class__.__name__}: {exc}"
                ) from exc
            time.sleep(base_delay * (2 ** (attempt - 1)))
            continue

        try:
            data = response.json()
        except ValueError as exc:
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise HxMarketBridgeError(
                f"HX market bridge returned HTTP {response.status_code} "
                f"action={action or '<unknown>'} attempt={attempt}/{max_attempts} "
                "with non-JSON body"
            ) from exc

        if response.ok:
            if not isinstance(data, dict):
                raise HxMarketBridgeError("HX market bridge response must be a JSON object")
            return data

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
            time.sleep(base_delay * (2 ** (attempt - 1)))
            continue

        raise HxMarketBridgeError(
            f"HX market bridge returned HTTP {response.status_code} "
            f"action={action or '<unknown>'} attempt={attempt}/{max_attempts}: {data}"
        )

    raise HxMarketBridgeError(
        f"HX market bridge exhausted retries for action={action or '<unknown>'}"
    )


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
