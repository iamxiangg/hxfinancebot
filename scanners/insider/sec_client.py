from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MasterIndexFetch:
    day: date
    text: str


class SECClient:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        max_requests_per_second: float = 5.0,
        retry_limit: int = 1,
    ) -> None:
        self.user_agent = (user_agent or os.getenv("SEC_USER_AGENT", "")).strip() or "hxfinancebot/1.0"
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self.timeout_seconds = timeout_seconds
        self.max_requests_per_second = max(0.1, max_requests_per_second)
        self.retry_limit = max(0, retry_limit)
        self._last_request_at = 0.0
        self._cache: dict[str, str] = {}

    def _throttle(self) -> None:
        minimum_interval = 1.0 / self.max_requests_per_second
        wait = minimum_interval - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get_text(self, url: str) -> str:
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        for attempt in range(self.retry_limit + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
                self._last_request_at = time.time()
                if response.status_code == 404:
                    raise FileNotFoundError(url)
                response.raise_for_status()
                self._cache[url] = response.text
                return response.text
            except FileNotFoundError:
                raise
            except requests.RequestException as exc:
                logger.warning("SEC request failed for %s: %s", url, exc.__class__.__name__)
                if attempt >= self.retry_limit:
                    raise
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"Failed to fetch {url}")

    def daily_master_index_url(self, day: date) -> str:
        quarter = ((day.month - 1) // 3) + 1
        return (
            "https://www.sec.gov/Archives/edgar/daily-index/"
            f"{day.year}/QTR{quarter}/master.{day.strftime('%Y%m%d')}.idx"
        )

    def fetch_daily_master_index(self, day: date) -> MasterIndexFetch:
        return MasterIndexFetch(day=day, text=self._get_text(self.daily_master_index_url(day)))

    def fetch_filing_text(self, archive_path: str) -> str:
        url = archive_path if archive_path.startswith("https://") else f"https://www.sec.gov/Archives/{archive_path.lstrip('/')}"
        return self._get_text(url)
