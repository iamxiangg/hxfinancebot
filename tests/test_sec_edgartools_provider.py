from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from providers.sec import get_sec_provider
from providers.sec.edgartools_provider import EdgarToolsSECProvider
from providers.sec.errors import SECProviderUnavailableError


class EdgarToolsProviderTests(unittest.TestCase):
    def test_lazy_import_raises_clear_error_when_package_missing(self) -> None:
        with patch("builtins.__import__", side_effect=ImportError("missing")):
            with self.assertRaises(SECProviderUnavailableError):
                EdgarToolsSECProvider()

    @patch.dict(os.environ, {"SEC_PROVIDER": "edgartools"}, clear=False)
    @patch("providers.sec.EdgarToolsSECProvider", return_value="provider")
    def test_factory_selects_edgartools_provider(self, mock_provider) -> None:
        provider = get_sec_provider()

        self.assertEqual(provider, "provider")
        mock_provider.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

