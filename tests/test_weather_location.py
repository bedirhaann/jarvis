"""Weather default-location privacy tests: personal location is loaded from a
private, git-ignored override and never hard-coded in the repository."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.weather_service import (  # noqa: E402
    DEFAULT_LATITUDE,
    DEFAULT_LOCATION_NAME,
    WeatherService,
    load_local_location_override,
)


def _no_network(_url):
    raise AssertionError("network must not be used in this test")


class TestWeatherLocationOverride(unittest.TestCase):
    def test_missing_override_falls_back_to_generic_city_default(self):
        svc = WeatherService(
            http_get_json=_no_network,
            location_config_path=Path("/nonexistent/jarvis/location.json"),
        )
        self.assertEqual(svc.default_location.display_name, DEFAULT_LOCATION_NAME)
        self.assertEqual(svc.default_location.latitude, DEFAULT_LATITUDE)

    def test_json_override_and_aliases_resolve_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "location.json"
            cfg.write_text(
                json.dumps({"name": "Test Mahallesi", "latitude": 10.5,
                            "longitude": 20.25, "aliases": ["Şükrü Köyü"]}),
                encoding="utf-8",
            )
            svc = WeatherService(http_get_json=_no_network, location_config_path=cfg)
            self.assertEqual(svc.default_location.display_name, "Test Mahallesi")
            resolved = svc.resolve_location("sukru koyu")
            self.assertEqual((resolved.latitude, resolved.longitude), (10.5, 20.25))

    def test_invalid_override_is_ignored_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "location.json"
            cfg.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_local_location_override(cfg, environ={}))
            cfg.write_text(json.dumps({"latitude": 999, "longitude": 0}), encoding="utf-8")
            self.assertIsNone(load_local_location_override(cfg, environ={}))

    def test_env_override_has_priority(self):
        result = load_local_location_override(
            Path("/nonexistent"),
            environ={"JARVIS_LOCATION_LAT": "1.5", "JARVIS_LOCATION_LON": "2.5",
                     "JARVIS_LOCATION_NAME": "Env"},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0].display_name, "Env")

    def test_explicit_constructor_coordinates_still_supported(self):
        svc = WeatherService("X", 1.0, 2.0, http_get_json=_no_network)
        self.assertEqual((svc.default_location.display_name,
                          svc.default_location.latitude), ("X", 1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
