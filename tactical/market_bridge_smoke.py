from __future__ import annotations

import json

from providers.hx_market_bridge import get_eligible_universe, ping


def main() -> int:
    health = ping()
    universe = get_eligible_universe()
    if health.get("status") != "OK":
        raise RuntimeError(f"Unexpected bridge health response: {health}")
    if not universe:
        raise RuntimeError("HX market bridge returned an empty eligible universe")
    print(json.dumps({"health": health, "eligible_count": len(universe)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
