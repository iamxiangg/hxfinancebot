from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from providers.sec import get_sec_provider
from providers.sec.cache import JSONDiskCache
from providers.sec.errors import SECNotFoundError
from providers.sec.official import OfficialSECProvider, TICKER_MAP_URL
from providers.sec.models import FilingMetadata


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data=None) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON payload")
        return self._json_data


class _FakeSession:
    def __init__(self, responses_by_url):
        self.responses_by_url = {key: list(value) for key, value in responses_by_url.items()}
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url: str, timeout: float):
        self.calls.append(url)
        queue = self.responses_by_url.get(url, [])
        if not queue:
            raise AssertionError(f"Unexpected URL: {url}")
        return queue.pop(0)


class ModuleGlobalThrottleTests(unittest.TestCase):
    """Behaviour tests for the process-global SEC throttle lock implemented in
    ``providers/sec/official.py``.

    Without the lock, two ``OfficialSECProvider`` instances in two threads
    share no rate-limit knowledge: each one sees its own
    ``self._last_request_at`` (the old per-instance attribute) and so each
    holds an isolated view of "last request". With ``max_requests_per_second=1``
    and 3 calls each, two isolated instances both complete their three
    requests in ~3 seconds; with the module-global lock the two instances share
    a single 1 req/sec slot and the same 3-each workload takes ~6 seconds.

    ``setUp`` resets ``_PROCESS_LAST_REQUEST_AT`` so test order is decoupled
    from any previous test that might have advanced the global tracker.
    """

    def setUp(self) -> None:
        from providers.sec import official as official_module
        official_module._PROCESS_LAST_REQUEST_AT = 0.0

    def _make_provider(self, *, kind: str) -> tuple[OfficialSECProvider, _FakeSession]:
        """Two independent sessions, each with 3 canned responses so a thread
        can fire 3 calls back-to-back without exhausting its queue."""
        responses = [
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
        ]
        session = _FakeSession({TICKER_MAP_URL: responses})
        provider = OfficialSECProvider(
            user_agent="hxfinancebot-tests/test@example.com",
            session=session,
            cache=JSONDiskCache(Path("unused"), default_ttl=timedelta(hours=1), enabled=False),
            cache_enabled=False,
            max_requests_per_second=1,
        )
        return provider, session

    def test_two_instances_share_process_global_throttle(self) -> None:
        """Two providers in two threads firing 3 calls each at
        ``max_requests_per_second=1`` must take at least 5 seconds total (six
        slots at 1Hz numbered 1..6 implies 5 inter-slot gaps of ~1s). Isolated
        per-instance throttles would finish at ~3s; the global lock holds the
        schedule.
        """
        provider_a, session_a = self._make_provider(kind="A")
        provider_b, session_b = self._make_provider(kind="B")

        def fire(p: OfficialSECProvider, n: int) -> None:
            for _ in range(n):
                p.company_profile("TEAM")

        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="throttle-test") as pool:
            pool.submit(fire, provider_a, 3)
            pool.submit(fire, provider_b, 3)
        elapsed = time.monotonic() - start

        # Both providers served their 3 calls (no spurious request counts).
        self.assertEqual(len(session_a.calls), 3)
        self.assertEqual(len(session_b.calls), 3)

        # Global lock holds the schedule: 6 calls at 1 req/sec = >= ~5.5s of
        # cumulative slot-space. We assert >= 5.0 to tolerate small jitter
        # while still ruling out the ~3s the per-instance throttle would yield.
        self.assertGreaterEqual(
            elapsed,
            5.0,
            f"Expected ~6s of wall-clock for shared throttle; got {elapsed:.2f}s. "
            "If this is closer to ~3s the process-global lock has regressed.",
        )

    def test_pre_reservation_records_slot_at_throttle_start(self) -> None:
        """The new ``_throttle`` reserves its slot at the START of the request
        via ``_PROCESS_LAST_REQUEST_AT``, not on response completion. A single
        provider firing 5 back-to-back requests at 10 req/sec must advance the
        global tracker by ~0.5s, proving reservations are timestamped
        regardless of how long each request took to complete.
        """
        from providers.sec import official as official_module

        responses = [
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
            _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
        ]
        session = _FakeSession({TICKER_MAP_URL: responses})
        provider = OfficialSECProvider(
            user_agent="hxfinancebot-tests/test@example.com",
            session=session,
            cache=JSONDiskCache(Path("unused"), default_ttl=timedelta(hours=1), enabled=False),
            cache_enabled=False,
            max_requests_per_second=10,
        )

        start = time.monotonic()
        for _ in range(5):
            provider.company_profile("TEAM")
        elapsed = time.monotonic() - start

        # 5 reservations at 10 req/sec (0.1s slot spacing) means the global
        # tracker has advanced by >= ~0.5s above ``start``. Network IO is
        # mocked at sub-millisecond, so wall-clock should mostly reflect the
        # serialised throttle slots + the tiny sleep outside the lock. We
        # assert ``start + 0.4`` instead of the theoretical ``start + 0.5``
        # to tolerate timer-jitter noise from GitHub-hosted runners.
        self.assertGreater(
            official_module._PROCESS_LAST_REQUEST_AT,
            start + 0.4,
            "Expected the global tracker to record request slots, not idle time.",
        )
        # Sanity: 5 calls at 10/sec means a ~0.4s minimum gap. Allow a generous
        # upper bound (CI runners are noisy) -- the meaningful assertion is
        # the LOW FLOOR proving the throttle is engaged.
        self.assertLess(elapsed, 5.0, "Throttle should still be fast under mock latency.")


class OfficialSECProviderTests(unittest.TestCase):
    def _provider(self, responses_by_url, *, cache_enabled: bool = False):
        session = _FakeSession(responses_by_url)
        provider = OfficialSECProvider(
            user_agent="hxfinancebot-tests/test@example.com",
            session=session,
            cache=JSONDiskCache(Path("unused"), default_ttl=timedelta(hours=1), enabled=cache_enabled),
            cache_enabled=cache_enabled,
            max_requests_per_second=10_000,
        )
        return provider, session

    def test_missing_user_agent_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = OfficialSECProvider()
            self.assertIn("hxfinancebot/1.0 (contact@hxfinancebot.dev)", provider.user_agent)

    def test_company_profile_and_recent_filing_normalization(self) -> None:
        submissions_url = "https://data.sec.gov/submissions/CIK0001650372.json"
        provider, _session = self._provider(
            {
                TICKER_MAP_URL: [
                    _FakeResponse(json_data={"0": {"ticker": "team", "cik_str": 1650372, "title": "Atlassian Corp"}}),
                    _FakeResponse(json_data={"0": {"ticker": "team", "cik_str": 1650372, "title": "Atlassian Corp"}}),
                ],
                submissions_url: [
                    _FakeResponse(
                        json_data={
                            "filings": {
                                "recent": {
                                    "accessionNumber": [
                                        "000165037224000123",
                                        "0001650372-24-000124",
                                        "0001650372-24-000125",
                                        "0001650372-24-000126",
                                        "0001650372-24-000127",
                                    ],
                                    "form": ["10-K", "10-Q", "8-K", "4", "4/A"],
                                    "filingDate": [
                                        "2024-08-01",
                                        "2024-05-01",
                                        "2024-04-15",
                                        "2024-04-10",
                                        "2024-04-11",
                                    ],
                                    "reportDate": [
                                        "2024-06-30",
                                        "2024-03-31",
                                        "",
                                        "2024-04-09",
                                        "2024-04-09",
                                    ],
                                    "primaryDocument": ["a10k.htm", "a10q.htm", "event8k.htm", "xslF345.xml", "xslF345A.xml"],
                                }
                            }
                        }
                    )
                ],
            }
        )

        profile = provider.company_profile("team")
        filings = provider.recent_filings("TEAM")

        self.assertEqual(profile.ticker, "TEAM")
        self.assertEqual(profile.cik, "0001650372")
        self.assertEqual([item.form for item in filings], ["10-K", "10-Q", "8-K", "4", "4/A"])
        # ``_normalize_accession`` reformats an 18-digit un-hyphenated string
        # into the canonical ``CIK-YY-NNNNNN`` shape; the fixture feeds an
        # 18-digit raw value so we expect the hyphen-reformatted result here.
        self.assertEqual(filings[0].accession, "0001650372-24-000123")
        self.assertTrue(filings[-1].is_amendment)

    def test_daily_index_filing_metadata_normalization(self) -> None:
        day = date(2024, 8, 1)
        day_url = "https://www.sec.gov/Archives/edgar/daily-index/2024/QTR3/master.20240801.idx"
        provider, _session = self._provider(
            {
                day_url: [
                    _FakeResponse(
                        text="\n".join(
                            [
                                "Description:",
                                "--------------------------------------------------------------------------------",
                                "1650372|Atlassian Corp|10-K|2024-08-01|edgar/data/1650372/0001650372-24-000123.txt",
                                "1650372|Atlassian Corp|10-Q|2024-08-01|edgar/data/1650372/0001650372-24-000124.txt",
                                "1650372|Atlassian Corp|8-K|2024-08-01|edgar/data/1650372/0001650372-24-000125.txt",
                                "1650372|Atlassian Corp|4|2024-08-01|edgar/data/1650372/0001650372-24-000126.txt",
                                "1650372|Atlassian Corp|4/A|2024-08-01|edgar/data/1650372/0001650372-24-000127.txt",
                            ]
                        )
                    )
                ]
            }
        )

        filings = provider.daily_index_filings(day)

        self.assertEqual([item.form for item in filings], ["10-K", "10-Q", "8-K", "4", "4/A"])
        self.assertTrue(filings[-1].is_amendment)

    def test_company_facts_point_in_time_filters_and_dedupes(self) -> None:
        submissions_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001650372.json"
        provider, _session = self._provider(
            {
                TICKER_MAP_URL: [_FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}})],
                submissions_url: [
                    _FakeResponse(
                        json_data={
                            "facts": {
                                "us-gaap": {
                                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                                        "units": {
                                            "USD": [
                                                {
                                                    "start": "2024-01-01",
                                                    "end": "2024-03-31",
                                                    "val": 100,
                                                    "fy": 2024,
                                                    "fp": "Q1",
                                                    "form": "10-Q",
                                                    "filed": "2024-04-15",
                                                    "accn": "0001650372-24-000111",
                                                },
                                                {
                                                    "start": "2024-01-01",
                                                    "end": "2024-03-31",
                                                    "val": 110,
                                                    "fy": 2024,
                                                    "fp": "Q1",
                                                    "form": "10-Q/A",
                                                    "filed": "2024-05-01",
                                                    "accn": "0001650372-24-000112",
                                                },
                                                {
                                                    "start": "2024-04-01",
                                                    "end": "2024-06-30",
                                                    "val": 130,
                                                    "fy": 2024,
                                                    "fp": "Q2",
                                                    "form": "10-Q",
                                                    "filed": "2024-08-01",
                                                    "accn": "0001650372-24-000113",
                                                },
                                            ],
                                            "EUR": [
                                                {
                                                    "start": "2024-01-01",
                                                    "end": "2024-03-31",
                                                    "val": 90,
                                                    "fy": 2024,
                                                    "fp": "Q1",
                                                    "form": "10-Q",
                                                    "filed": "2024-04-15",
                                                    "accn": "0001650372-24-000114",
                                                }
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    )
                ],
            }
        )

        facts = provider.company_facts("TEAM", as_of=datetime(2024, 6, 1, tzinfo=UTC))
        revenue_facts = facts.facts["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"]

        self.assertEqual(len(revenue_facts), 2)
        usd_fact = next(item for item in revenue_facts if item.unit == "USD")
        self.assertEqual(usd_fact.value, 110)
        self.assertEqual(usd_fact.accession, "0001650372-24-000112")

    def test_filing_document_discovery_and_text_retrieval(self) -> None:
        filing = self._filing("0001650372-24-000123")
        source_url = filing.source_url
        primary_url = "https://www.sec.gov/Archives/edgar/data/1650372/000165037224000123/main.htm"
        provider, _session = self._provider(
            {
                source_url: [
                    _FakeResponse(
                        text="""
<DOCUMENT>
<TYPE>10-Q</TYPE>
<SEQUENCE>1</SEQUENCE>
<FILENAME>main.htm</FILENAME>
<DESCRIPTION>Quarterly report</DESCRIPTION>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-99.1</TYPE>
<SEQUENCE>2</SEQUENCE>
<FILENAME>ex99-1.htm</FILENAME>
<DESCRIPTION>Press release</DESCRIPTION>
</DOCUMENT>
"""
                    ),
                    _FakeResponse(
                        text="""
<DOCUMENT>
<TYPE>10-Q</TYPE>
<SEQUENCE>1</SEQUENCE>
<FILENAME>main.htm</FILENAME>
<DESCRIPTION>Quarterly report</DESCRIPTION>
</DOCUMENT>
"""
                    ),
                ],
                primary_url: [_FakeResponse(text="<html>main doc</html>")],
            }
        )

        documents = provider.filing_documents(filing)
        primary = provider.filing_text(filing)

        self.assertEqual(len(documents), 2)
        self.assertTrue(documents[0].is_primary)
        self.assertEqual(primary.text, "<html>main doc</html>")

    def test_form4_transactions_are_normalized(self) -> None:
        filing = self._filing("0001650372-24-000124", form="4/A", primary_document="index.txt")
        source_url = filing.source_url
        xml_url = "https://www.sec.gov/Archives/edgar/data/1650372/000165037224000124/ownership.xml"
        provider, _session = self._provider(
            {
                source_url: [
                    _FakeResponse(
                        text="""
<DOCUMENT>
<TYPE>XML</TYPE>
<FILENAME>ownership.xml</FILENAME>
</DOCUMENT>
"""
                    )
                ],
                xml_url: [
                    _FakeResponse(
                        text="""<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>1650372</issuerCik>
    <issuerTradingSymbol>TEAM</issuerTradingSymbol>
  </issuer>
  <periodOfReport>2024-04-09</periodOfReport>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>2002</rptOwnerCik>
      <rptOwnerName>Jane Doe</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-04-09</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>42.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>11000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
                    )
                ],
            }
        )

        transactions = provider.form4_transactions(filing)

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].ticker, "TEAM")
        self.assertEqual(transactions[0].owner_name, "Jane Doe")
        self.assertEqual(transactions[0].transaction_code, "P")

    @patch("providers.sec.official.time.sleep")
    def test_retry_on_429_and_5xx(self, _mock_sleep) -> None:
        provider, session = self._provider(
            {
                TICKER_MAP_URL: [
                    _FakeResponse(status_code=429, json_data={"error": "slow down"}),
                    _FakeResponse(status_code=500, json_data={"error": "server"}),
                    _FakeResponse(json_data={"0": {"ticker": "TEAM", "cik_str": 1650372, "title": "Atlassian Corp"}}),
                ]
            }
        )

        profile = provider.company_profile("TEAM")

        self.assertEqual(profile.cik, "0001650372")
        self.assertEqual(len(session.calls), 3)

    def test_no_retry_on_404(self) -> None:
        provider, session = self._provider({TICKER_MAP_URL: [_FakeResponse(status_code=404, text="missing")]})

        with self.assertRaises(SECNotFoundError):
            provider.company_profile("TEAM")

        self.assertEqual(len(session.calls), 1)

    @patch.dict(os.environ, {"SEC_PROVIDER": "official", "SEC_USER_AGENT": "hxfinancebot-tests/test@example.com"}, clear=False)
    def test_factory_selects_official_provider(self) -> None:
        with patch("providers.sec.OfficialSECProvider", return_value="provider") as mock_provider:
            provider = get_sec_provider()

        self.assertEqual(provider, "provider")
        mock_provider.assert_called_once_with()

    def _filing(self, accession: str, *, form: str = "10-Q", primary_document: str = "main.htm") -> FilingMetadata:
        return FilingMetadata(
            ticker="TEAM",
            cik="0001650372",
            accession=accession,
            form=form,
            filed_at=datetime(2024, 8, 1, tzinfo=UTC),
            report_date=date(2024, 6, 30),
            primary_document=primary_document,
            is_amendment=form.endswith("/A"),
            source_url=f"https://www.sec.gov/Archives/edgar/data/1650372/{accession.replace('-', '')}/{accession}.txt",
        )


class UserAgentWarningDedupTests(unittest.TestCase):
    """When ``SEC_USER_AGENT`` is unset, the provider falls back to a default
    AND logs a WARNING. The warning dedups within a single Python process so
    repeated ``OfficialSECProvider()`` constructions during one scan don't
    spam the workflow log on every cron run -- the first occurrence is loud,
    subsequent ones within the same process are silently suppressed.
    """

    def setUp(self) -> None:
        # Reset the module-level dedup flag so each test starts in a fresh state.
        from providers.sec import official as official_module
        official_module._USER_AGENT_WARN_EMITTED = False

    def test_first_construction_without_user_agent_emits_warning(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertLogs("providers.sec.official", level="WARNING") as captured:
                OfficialSECProvider()
        self.assertTrue(
            any(
                "SEC_USER_AGENT is not configured" in line
                for line in captured.output
            )
        )

    def test_second_construction_in_same_process_does_not_emit_warning(self) -> None:
        """Dedup: first construction emits and locks the flag; a subsequent
        construction in the same process must NOT emit."""
        # First construction: emits and locks the flag.
        with patch.dict(os.environ, {}, clear=True):
            OfficialSECProvider()

        # Second construction: deduped. ``assertLogs`` raises on
        # no-logs-captured, so install a custom handler for negative
        # assertions.
        captured_records: list = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_records.append(record)

        handler = _Capture(level=logging.WARNING)
        target_logger = logging.getLogger("providers.sec.official")
        target_logger.addHandler(handler)
        try:
            with patch.dict(os.environ, {}, clear=True):
                OfficialSECProvider()
        finally:
            target_logger.removeHandler(handler)

        self.assertFalse(
            any(
                "SEC_USER_AGENT is not configured" in r.getMessage()
                for r in captured_records
            ),
            "second construction should NOT emit warning once flag is set",
        )

    def test_warning_with_user_agent_set_does_not_emit(self) -> None:
        """If the user provides SEC_USER_AGENT, no warning at all -- including
        on the FIRST construction -- so we never spam even log lines without
        value to the operator."""
        captured_records: list = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_records.append(record)

        handler = _Capture(level=logging.WARNING)
        target_logger = logging.getLogger("providers.sec.official")
        target_logger.addHandler(handler)
        try:
            with patch.dict(
                os.environ,
                {"SEC_USER_AGENT": "hxfinancebot-tests/test@example.com"},
                clear=True,
            ):
                OfficialSECProvider()
        finally:
            target_logger.removeHandler(handler)

        self.assertFalse(
            any(
                "SEC_USER_AGENT is not configured" in r.getMessage()
                for r in captured_records
            )
        )


if __name__ == "__main__":
    unittest.main()
