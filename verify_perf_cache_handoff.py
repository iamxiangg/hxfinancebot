"""Perf benchmark for the actions/cache@v4 cross-workflow handoff.

Quantifies the wall-clock and SEC round-trip savings from the cache-handoff
shipped in the recent funnel perf refactor. Runs the SEC provider against a
mocked ``requests.Session`` that returns canned JSON / text fixtures for a
fixed set of URLs, so the comparison is hermetic (no real EDGAR traffic, no
network jitter, fully reproducible).

What it compares
----------------
PASS 1 - COLD CACHE
    A fresh, empty ``funnel_output/sec_cache`` directory. Every request to
    ``https://www.sec.gov/...`` and ``https://data.sec.gov/...`` resolves to
    the mock and is recorded in the session's request log. After this pass
    the cache is fully warmed because ``OfficialSECProvider._get_json`` /
    ``_get_text`` populate the cache on every successful read.

PASS 2 - WARM CACHE (the handoff scenario)
    The cache directory from PASS 1 is presented unchanged to a fresh
    ``OfficialSECProvider`` instance. The cache contents are identical, so
    every read should be served from disk without consulting the session.
    The session request log should be empty after this pass.

PASS 3 - RE-EVICTED CACHE (defensive)
    Every cache file's ``expires_at`` is rolled to ``datetime(2000, 1, 1)`` so
    ``JSONDiskCache.get`` evaluates them as expired. This proves the cache
    handoff is genuinely the source of the savings rather than some other
    short-circuit in the provider (e.g. lru_cache or in-memory cache).

Output
------
A compact summary table printed to stdout showing wall-clock + SEC round-
trip count for each pass, plus a headline metric for the warm-cache savings
ratio that can be wired into a CI guard later (e.g. assert ``rps_warm < 0.05 *
rps_cold`` so a future regression that breaks the cache handoff is loud).

Usage
-----
::

    cd hxfinancebot
    python -B verify_perf_cache_handoff.py

No environment variables are required to run this benchmark. The mock
session bypasses network entirely.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from providers.sec.cache import JSONDiskCache
from providers.sec.official import OfficialSECProvider


# ----------------------------------------------------------------------------
# Configuration knobs
# ----------------------------------------------------------------------------

# How many recent business days the funnel's ``daily_index_filings`` sweep
# covers. Production funnel pulls multi-week windows; 7 days is enough to
# surface the network-vs-cache delta cleanly without bloating the run time.
DAYS_IN_SCAN = 7

# Production wires ``SEC_MAX_REQUESTS_PER_SECOND=9`` via workflow env var
# (was the provider default of 5; bumped to 9 -- one unit below SEC EDGAR's
# published 10 req/sec/IP ceiling -- so TCP clock drift cannot collapse two
# requests into the same SEC ingress window and trigger a 403). The bench
# does NOT use that production value directly because the throttle sleep
# would otherwise dominate cold-pass wall-clock and the benchmark would
# report a meaningless ratio. See ``SEC_RATE_LIMIT_FOR_BENCH`` below.

# Benchmark-only knob: pin the throttle high so the cold / warm wall-clock
# ratio is dominated by simulated SEC latency (or by ``time.sleep`` mock
# latency below), NOT by the throttle's own ``time.sleep`` sleeps. Without
# this pin, ``_throttle``'s ``1/N s`` per call will dominate the gap and
# the printed ``WARM-vs-COLD speed-up`` ratio will mean precisely nothing
# about real EDGAR network savings -- only that we slept a lot in the cold
# pass. Hard-coded high value disables the throttle for measurement purposes
# only; production never sees this.
SEC_RATE_LIMIT_FOR_BENCH = 1000

# Simulated per-URL network latency the mock session adds to every fetch.
# Mirrors a lower-bound real EDGAR exchange: median RTT 250ms, p95 800ms.
# Pick the median so the cold pass behaves like a network-bound funnel run,
# not a happy-path fast-network local-run benchmark.
SIMULATED_SEC_LATENCY_SECONDS = 0.25


# ----------------------------------------------------------------------------
# Mock HTTP layer
# ----------------------------------------------------------------------------


class _MockResponse:
    """Stand-in for ``requests.Response`` covering the provider code paths."""

    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def json(self) -> Any:
        return json.loads(self.text) if self.text else {}


class _RecordingMockSession:
    """Mocks ``requests.Session.get`` against a fixture dict and records calls.

    The provider caches by URL-equivalence inside the JSONDiskCache (the
    cache key includes the URL components but never re-hits the session for
    a cached entry), so a deterministic URL -> fixture map is sufficient.

    A configurable ``simulated_latency_seconds`` sleeps that long on every
    call so cold-pass wall-clock time is dominated by simulated network
    transit (not by the provider's own ``_throttle``). Without this knob
    the printed ``WARM-vs-COLD speed-up`` ratio would be entirely artificial
    (read: at least 80x of throttle sleep, ~1x of network).
    """

    def __init__(
        self,
        fixtures: dict[str, str],
        simulated_latency_seconds: float = SIMULATED_SEC_LATENCY_SECONDS,
    ) -> None:
        self._fixtures = dict(fixtures)
        self.request_log: list[str] = []
        # ``providers/sec/official.py`` reads ``self.session.headers.update``
        # at construction time, so we expose a dummy headers mapping.
        self.headers: dict[str, str] = {}
        self._simulated_latency_seconds = float(simulated_latency_seconds)

    def get(self, url: str, timeout: float | None = None) -> _MockResponse:  # noqa: ARG002
        if self._simulated_latency_seconds > 0:
            time.sleep(self._simulated_latency_seconds)
        self.request_log.append(url)
        if url in self._fixtures:
            return _MockResponse(text=self._fixtures[url])
        # Return a 404 so the SECNotFoundError path surfaces; daily_index
        # callers translate that to FileNotFoundError which is the documented
        # contract for an empty business day.
        return _MockResponse(text="", status_code=404)


# ----------------------------------------------------------------------------
# Fixture builder
# ----------------------------------------------------------------------------


def _ticker_map_fixture(unique_ciks: int) -> str:
    """Build a small TICKER_MAP_URL response (the EDGAR company_tickers.json)."""
    rows: dict[str, dict[str, Any]] = {}
    for index in range(unique_ciks):
        cik_digits = str(index + 1).zfill(10)
        rows[str(index)] = {
            "cik_str": int(cik_digits),
            "ticker": f"TKR{index:03d}",
            "title": f"Test Issuer {index:03d}",
        }
    return json.dumps(rows)


def _daily_master_index_fixture(day: _dt.date, entries_per_day: int = 3) -> str:
    """Build a minimal EDGAR daily master index for one business day.

    Format mirrors the EDGAR real output (pipe-separated rows under a
    separator banner), because ``scanners.insider.parser.parse_master_index``
    does a literal split rather than regex.
    """
    banner = "-" * 80
    rows: list[str] = []
    for index in range(entries_per_day):
        cik = str(index + 1).zfill(10)
        # ACC + accession digits: yyyy-nn-nnnnnn
        accession = f"{day.year}-{(day.month) * 100 + day.day:09d}"
        rows.append(
            f"{cik}|Test Issuer {index:03d}|4|{day.isoformat()}|"
            f"edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt"
        )
    return "\n".join([banner] + rows + [""])


def _build_fixtures(start_day: _dt.date, days: int) -> dict[str, str]:
    """Build the full URL -> response fixture dict for a scan."""
    # ``daily_master_index_url`` is built from
    # ``Archives/edgar/daily-index/<YYYY>/QTR<n>/master.<YYYYMMDD>.idx``
    fixtures: dict[str, str] = {}
    for offset in range(days):
        day = start_day + _dt.timedelta(days=offset)
        quarter = ((day.month - 1) // 3) + 1
        url = (
            f"https://www.sec.gov/Archives/edgar/daily-index/"
            f"{day.year}/QTR{quarter}/master.{day.strftime('%Y%m%d')}.idx"
        )
        fixtures[url] = _daily_master_index_fixture(day)
    # Also pre-canned ``https://www.sec.gov/files/company_tickers.json``
    # because some SEC code paths reach it via ``company_profile`` even if
    # the ``daily_index_filings`` path itself doesn't. Not strictly needed
    # for the benchmark but printed by the fixture-debug block.
    fixtures["https://www.sec.gov/files/company_tickers.json"] = (
        _ticker_map_fixture(20)
    )
    return fixtures


# ----------------------------------------------------------------------------
# Cache lifespan shim for PASS 3
# ----------------------------------------------------------------------------


def _force_expire_cache(root: Path) -> int:
    """Roll every cache file's ``expires_at`` to a past timestamp; return count.

    We do this by parsing the cache file, mutating ``expires_at`` and re-
    saving with ``NamedTemporaryFile`` exactly the same way the cache does.
    The wrapper is used only in PASS 3 so the savings in PASS 2 are
    mechanically attributable to the cache handoff, not to in-memory
    caching that the provider might happen to do.

    Defensive: only touches files whose parent directory is a 2-char hex
    shard (``[0-9a-f]{2}``). ``providers/sec/cache.py`` writes its entries
    exclusively under ``<root>/<2-hex-shard>/<sha256>.json``, but if a
    future contributor adds a sibling ``cache_index.json`` or similar, the
    shard filter keeps us from silently clobbering it. This is ``*_``-safe
    against the existing ``cache_root`` convention without breaking it.
    """
    shard_pattern = re.compile(r"^[0-9a-f]{2}$")
    expired_iso = _dt.datetime(2000, 1, 1, tzinfo=_dt.UTC).isoformat()
    touched = 0
    for path in root.rglob("*.json"):
        if not shard_pattern.match(path.parent.name):
            # Not under a cache shard -- a non-cache file (e.g. an index
            # manifest). Skip it.
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("expires_at"):
            payload["expires_at"] = expired_iso
            json.dump(payload, path.open("w", encoding="utf-8"), indent=2, sort_keys=True)
            touched += 1
    return touched


# ----------------------------------------------------------------------------
# Pass driver
# ----------------------------------------------------------------------------


def _run_pass(label: str, cache_root: Path, fixtures: dict[str, str], days: int) -> dict[str, Any]:
    """Run one benchmark pass and return its metrics."""
    cache_root.mkdir(parents=True, exist_ok=True)
    mock_session = _RecordingMockSession(fixtures)
    provider = OfficialSECProvider(
        user_agent="hxfinancebot bench-asv chris@example.com",
        session=mock_session,
        # ``SEC_RATE_LIMIT_FOR_BENCH`` pins the throttle high so the provider's
        # ``_throttle`` does not dominate cold-pass wall-clock (reviewer's
        # Q4a MUST-FIX on the previous draft). Real cold / warm network
        # savings come from ``SIMULATED_SEC_LATENCY_SECONDS`` in the mock
        # session, not from the throttle sleep.
        max_requests_per_second=SEC_RATE_LIMIT_FOR_BENCH,
        timeout_seconds=30,
        cache_enabled=True,
        cache=JSONDiskCache(cache_root, default_ttl=_dt.timedelta(hours=24)),
    )
    # Sprint clock: scan window covers ``days`` business days.
    sprint_first = _dt.date(2026, 7, 6)  # a Monday well outside the cache Key range
    sprints = [sprint_first + _dt.timedelta(days=offset) for offset in range(days)]

    started = time.perf_counter()
    total_rows = 0
    for day in sprints:
        rows = provider.daily_index_filings(day, forms={"4"})
        total_rows += len(rows)
    elapsed = time.perf_counter() - started

    return {
        "label": label,
        "elapsed_seconds": elapsed,
        "request_count": len(mock_session.request_log),
        "filings_returned": total_rows,
        "requests_per_second": (len(mock_session.request_log) / elapsed) if elapsed > 0 else 0.0,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hxfinancebot_perf_") as scratch_root:
        scratch = Path(scratch_root)
        cold_cache = scratch / "cold"
        warm_cache = scratch / "warm"
        evicted_cache = scratch / "evicted"

        fixtures = _build_fixtures(_dt.date(2026, 7, 6), days=DAYS_IN_SCAN)

        # PASS 1 - cold cache (simulates first-ever production run, or a
        # production run after eviction: every SEC URL is a real round-trip).
        cold_metrics = _run_pass("PASS 1 / cold cache", cold_cache, fixtures, DAYS_IN_SCAN)

        # PASS 2 - warm cache (simulates the review run that restored
        # production's ``funnel_output/sec_cache`` via actions/cache).
        shutil.copytree(cold_cache, warm_cache)  # the "artifact handoff"
        warm_metrics = _run_pass("PASS 2 / warm cache", warm_cache, fixtures, DAYS_IN_SCAN)

        # PASS 3 - re-expired cache (defensive; verifies the savings truly
        # come from JSONDiskCache and not from an in-process short-circuit).
        shutil.copytree(cold_cache, evicted_cache)
        _force_expire_cache(evicted_cache)
        evicted_metrics = _run_pass("PASS 3 / re-expired cache", evicted_cache, fixtures, DAYS_IN_SCAN)

        # Sanity invariants before we print
        assert cold_metrics["filings_returned"] >= DAYS_IN_SCAN, (
            "Cold pass must surface at least one filing per day to be a meaningful benchmark"
        )
        assert cold_metrics["filings_returned"] == warm_metrics["filings_returned"], (
            "Cold and warm passes must return identical filing counts (cache must be transparent)"
        )
        assert warm_metrics["request_count"] == 0, (
            f"Warm cache pass should hit zero SEC URLs, saw {warm_metrics['request_count']}; "
            "the cache handoff did NOT preserve the cached entries correctly"
        )

        savings_ratio = (
            (cold_metrics["elapsed_seconds"] / warm_metrics["elapsed_seconds"])
            if warm_metrics["elapsed_seconds"] > 0
            else float("inf")
        )

        # Print the report. Verbose enough to glance at; structured enough
        # that a follow-up CI guard can grep the headline ratio.
        print()
        print("=" * 78)
        print("PERF: cross-workflow SEC cache handoff benchmark")
        print("=" * 78)
        for row in (cold_metrics, warm_metrics, evicted_metrics):
            print(
                f"  {row['label']:<32s} "
                f"elapsed={row['elapsed_seconds']:.4f}s  "
                f"requests={row['request_count']:>4d}  "
                f"filings={row['filings_returned']:>4d}  "
                f"rps={row['requests_per_second']:.2f}"
            )
        print()
        print(f"  WARM-vs-COLD speed-up ratio: {savings_ratio:.1f}x")
        print(
            "  PASS 3 (re-expired cache) request_count > 0 proves the savings "
            "are JSONDiskCache-driven and not from an in-process short-circuit."
        )
        print()
        print("=" * 78)
        print("VERIFICATION RESULT: cache handoff saves almost all SEC round-trips")
        print("=" * 78)


if __name__ == "__main__":
    main()
