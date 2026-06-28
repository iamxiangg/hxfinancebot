from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from providers.sec.cache import JSONDiskCache


class SECCacheTests(unittest.TestCase):
    def test_cache_hit_and_miss(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = JSONDiskCache(Path(temp_dir), default_ttl=timedelta(hours=1))

            self.assertIsNone(cache.get("missing"))
            cache.set("alpha", {"value": 1})

            self.assertEqual(cache.get("alpha"), {"value": 1})

    def test_corrupt_cache_recovers_cleanly(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = JSONDiskCache(Path(temp_dir), default_ttl=timedelta(hours=1))
            path = cache._path_for_key("broken")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json", encoding="utf-8")

            self.assertIsNone(cache.get("broken"))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

