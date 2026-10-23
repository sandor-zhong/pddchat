from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aftersale_result_settings as settings


class AftersaleResultSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temp) / "query.json"
        self.enterContext(patch.object(settings, "SETTINGS_PATH", self.path))

    def test_default_and_saved_interval_survive_reload(self) -> None:
        self.assertEqual(settings.load_query_interval(), 3)
        settings.save_query_interval(8)
        self.assertEqual(settings.load_query_interval(), 8)

    def test_invalid_configuration_falls_back_to_default(self) -> None:
        for text in ('broken', '[]', '{"query_interval_seconds": 0}', '{"query_interval_seconds": 90}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                self.assertEqual(settings.load_query_interval(), 3)

    def test_only_integer_seconds_in_supported_range_are_accepted(self) -> None:
        self.assertEqual(settings.validate_query_interval("6"), 6)
        for value in (0, -1, 61, None, True, 1.5, "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.validate_query_interval(value)


if __name__ == "__main__":
    unittest.main()
