"""JARVIS Local Weather Service — Zero-dependency Open-Meteo & Nominatim client."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


# Generic, city-level public default. A precise personal location must NOT be
# committed to the repository; it is loaded at runtime from a local, git-ignored
# override instead (see load_local_location_override()).
DEFAULT_LOCATION_NAME = "Ankara"
DEFAULT_LATITUDE = 39.9255
DEFAULT_LONGITUDE = 32.8663

# Fast preset coordinates for instant resolution without extra geocoding round-trip
PRESET_LOCATIONS: dict[str, tuple[str, float, float]] = {
    "cankaya": ("Ankara, Çankaya", 39.9035, 32.8597),
    "ankara": ("Ankara", 39.9255, 32.8663),
    "istanbul": ("İstanbul", 41.0082, 28.9784),
    "izmir": ("İzmir", 38.4237, 27.1428),
    "bursa": ("Bursa", 40.1885, 29.0610),
    "antalya": ("Antalya", 36.8969, 30.7133),
    "adana": ("Adana", 37.0000, 35.3213),
    "konya": ("Konya", 37.8746, 32.4932),
    "eskisehir": ("Eskişehir", 39.7767, 30.5206),
    "gaziantep": ("Gaziantep", 37.0662, 37.3833),
    "kayseri": ("Kayseri", 38.7312, 35.4787),
    "mersin": ("Mersin", 36.8121, 34.6415),
    "samsun": ("Samsun", 41.2867, 36.3300),
    "trabzon": ("Trabzon", 41.0027, 39.7168),
    "diyarbakir": ("Diyarbakır", 37.9144, 40.2306),
}

WMO_CODE_MAP: dict[int, tuple[str, str]] = {
    0: ("☀️", "Açık"),
    1: ("🌤️", "Çoğunlukla Açık"),
    2: ("⛅", "Parçalı Bulutlu"),
    3: ("☁️", "Çok Bulutlu"),
    45: ("🌫️", "Sisli"),
    48: ("🌫️", "Kırağılı Sis"),
    51: ("🌦️", "Hafif Çisenti"),
    53: ("🌦️", "Orta Şiddetli Çisenti"),
    55: ("🌧️", "Yoğun Çisenti"),
    56: ("🌧️", "Dondurucu Hafif Çisenti"),
    57: ("🌧️", "Dondurucu Yoğun Çisenti"),
    61: ("🌦️", "Hafif Yağmurlu"),
    63: ("🌧️", "Yağmurlu"),
    65: ("🌧️", "Kuvvetli Yağmurlu"),
    66: ("🌨️", "Dondurucu Hafif Yağmur"),
    67: ("🌨️", "Dondurucu Kuvvetli Yağmur"),
    71: ("🌨️", "Hafif Kar Yağışlı"),
    73: ("❄️", "Kar Yağışlı"),
    75: ("❄️", "Yoğun Kar Yağışlı"),
    77: ("❄️", "Kar Taneleri"),
    80: ("🌦️", "Hafif Sağanak Yağışlı"),
    81: ("🌧️", "Sağanak Yağışlı"),
    82: ("⛈️", "Kuvvetli Sağanak Yağışlı"),
    85: ("🌨️", "Hafif Kar Sağanağı"),
    86: ("❄️", "Yoğun Kar Sağanağı"),
    95: ("⛈️", "Gök Gürültülü Sağanak"),
    96: ("⛈️", "Dolu Eşliğinde Gök Gürültülü Sağanak"),
    99: ("⛈️", "Kuvvetli Dolu ve Gök Gürültülü Sağanak"),
}

TURKISH_WEEKDAYS = {
    0: "Pazartesi",
    1: "Salı",
    2: "Çarşamba",
    3: "Perşembe",
    4: "Cuma",
    5: "Cumartesi",
    6: "Pazar",
}

_TR_ASCII_MAP = str.maketrans({
    "ı": "i",
    "İ": "i",
    "I": "i",
    "ğ": "g",
    "Ğ": "g",
    "ü": "u",
    "Ü": "u",
    "ş": "s",
    "Ş": "s",
    "ö": "o",
    "Ö": "o",
    "ç": "c",
    "Ç": "c",
})

_WEATHER_COMMAND_RE = re.compile(
    r"^\s*(?:/plan\s+)?(?:/hava|/havadurumu|/weather)\b(.*)$",
    re.IGNORECASE,
)

_WEATHER_PHRASES = (
    "hava durumu",
    "havadurumu",
    "hava nasil",
    "havalar nasil",
    "hava kac derece",
    "kac derece",
    "hava tahmini",
    "yagmur yagacak mi",
    "yagis var mi",
    "semsiye alayim mi",
    "bugun hava",
    "yarin hava",
)

# Phrases that mean the user is talking about coding/modifying the weather feature itself
_META_DEVELOPMENT_PHRASES = (
    "ozelligini",
    "ozeliigini",
    "servis kullanarak",
    "servis kullanrak",
    "ekle",
    "kodla",
    "planla",
    "entegrasyon",
    "implement",
)

_STOPWORDS_FOR_LOCATION = (
    "/plan",
    "/hava",
    "/havadurumu",
    "/weather",
    "hava durumu",
    "havadurumu",
    "hava nasil",
    "havalar nasil",
    "hava kac derece",
    "kac derece",
    "hava tahmini",
    "yagmur yagacak mi",
    "yagis var mi",
    "semsiye alayim mi",
    "bugun",
    "yarin",
    "su an",
    "anlik",
    "nasil",
    "nedir",
    "acaba",
    "icin",
    "da",
    "de",
    "ta",
    "te",
    "hava",
)


def _normalize_tr(text: str) -> str:
    return (text or "").translate(_TR_ASCII_MAP).lower().strip()


def describe_wmo_code(code: int | None) -> tuple[str, str]:
    if code is None:
        return ("⛅", "Belirsiz")
    return WMO_CODE_MAP.get(int(code), ("⛅", "Parçalı Bulutlu"))


def _format_day_label(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = TURKISH_WEEKDAYS.get(dt.weekday(), date_str)
        return f"{weekday} ({dt.strftime('%d.%m')})"
    except ValueError:
        return date_str


@dataclass
class ResolvedLocation:
    display_name: str
    latitude: float
    longitude: float


def local_location_config_path() -> Path:
    """Path of the private, git-ignored home location file
    ($XDG_CONFIG_HOME/jarvis/location.json, default ~/.config/jarvis/location.json)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "jarvis" / "location.json"


def load_local_location_override(
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[ResolvedLocation, tuple[str, ...]] | None:
    """Load the user's personal default location from outside the repository.

    Priority: env vars JARVIS_LOCATION_NAME / JARVIS_LOCATION_LAT / JARVIS_LOCATION_LON,
    then the JSON file {"name": str, "latitude": float, "longitude": float,
    "aliases": [str, ...]}. Returns (location, normalized_aliases) or None when no
    valid override exists. Invalid data is ignored (never crashes the app)."""
    env = os.environ if environ is None else environ
    try:
        if env.get("JARVIS_LOCATION_LAT") and env.get("JARVIS_LOCATION_LON"):
            lat = float(env["JARVIS_LOCATION_LAT"])
            lon = float(env["JARVIS_LOCATION_LON"])
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                name = (env.get("JARVIS_LOCATION_NAME") or "Ev Konumu").strip()
                return ResolvedLocation(name, lat, lon), ()
    except (TypeError, ValueError):
        pass

    path = config_path if config_path is not None else local_location_config_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        name = str(data.get("name") or "Ev Konumu").strip()[:120]
        aliases = tuple(
            _normalize_tr(str(a)) for a in data.get("aliases", []) if str(a).strip()
        )
        return ResolvedLocation(name, lat, lon), aliases
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


@dataclass
class DailyForecastItem:
    date: str
    weather_code: int
    temp_min: float
    temp_max: float
    precip_prob: int


@dataclass
class WeatherSnapshot:
    location: ResolvedLocation
    temperature: float
    apparent_temperature: float
    humidity: int
    wind_speed: float
    weather_code: int
    daily: list[DailyForecastItem]

    @property
    def icon(self) -> str:
        return describe_wmo_code(self.weather_code)[0]

    @property
    def description(self) -> str:
        return describe_wmo_code(self.weather_code)[1]

    def format_badge(self) -> str:
        return f"{self.icon} {round(self.temperature)}°C"


class WeatherService:
    """Local weather service using Open-Meteo Forecast API and Nominatim/Open-Meteo Geocoding."""

    def __init__(
        self,
        default_location_name: str | None = None,
        default_latitude: float | None = None,
        default_longitude: float | None = None,
        http_get_json: Callable[[str], dict | list] | None = None,
        timeout: float = 6.0,
        location_config_path: Path | None = None,
    ):
        self._home_aliases: tuple[str, ...] = ()
        if default_latitude is not None and default_longitude is not None:
            self.default_location = ResolvedLocation(
                display_name=default_location_name or DEFAULT_LOCATION_NAME,
                latitude=default_latitude,
                longitude=default_longitude,
            )
        else:
            override = load_local_location_override(location_config_path)
            if override is not None:
                self.default_location, self._home_aliases = override
            else:
                self.default_location = ResolvedLocation(
                    display_name=DEFAULT_LOCATION_NAME,
                    latitude=DEFAULT_LATITUDE,
                    longitude=DEFAULT_LONGITUDE,
                )
        self.timeout = timeout
        self._http_get_json = http_get_json or self._default_http_get_json

    def _default_http_get_json(self, url: str) -> dict | list:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "JARVIS-Desktop-Assistant/1.0 (Linux; PySide6)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    @staticmethod
    def is_weather_query(message: str) -> bool:
        """Return True if the user message is asking for weather information."""
        if not message or not message.strip():
            return False

        if _WEATHER_COMMAND_RE.match(message):
            return True

        norm = _normalize_tr(message)
        if norm.startswith("/plan "):
            norm = norm[6:].strip()

        # Ignore requests about coding/implementing the weather feature itself
        if any(meta in norm for meta in _META_DEVELOPMENT_PHRASES):
            return False

        return any(phrase in norm for phrase in _WEATHER_PHRASES)

    @staticmethod
    def extract_location_query(message: str) -> str:
        """Extract clean location string from a command or natural language weather query.
        Returns empty string if no specific location was mentioned."""
        if not message:
            return ""

        cmd_match = _WEATHER_COMMAND_RE.match(message)
        if cmd_match:
            raw_loc = cmd_match.group(1).strip(" ?!.,;:-")
            return raw_loc

        cleaned = message.strip()
        if cleaned.lower().startswith("/plan"):
            cleaned = cleaned[5:].strip()

        # Remove punctuation
        cleaned = re.sub(r"[?!.,;:'\"()]+", " ", cleaned)

        # Strip common Turkish weather phrases using case-insensitive matching
        norm_tokens: list[str] = []
        working = cleaned
        # Replace multi-word weather phrases first
        multi_word_patterns = [
            r"\bhava\s+durumu\b",
            r"\bhavalar\s+nasıl\b",
            r"\bhavalar\s+nasil\b",
            r"\bhava\s+nasıl\b",
            r"\bhava\s+nasil\b",
            r"\bhava\s+kaç\s+derece\b",
            r"\bhava\s+kac\s+derece\b",
            r"\bkaç\s+derece\b",
            r"\bkac\s+derece\b",
            r"\bhava\s+tahmini\b",
            r"\byağmur\s+yağacak\s+mı\b",
            r"\byagmur\s+yagacak\s+mi\b",
            r"\byağış\s+var\s+mı\b",
            r"\byagis\s+var\s+mi\b",
            r"\bşemsiye\s+alayım\s+mı\b",
            r"\bsemsiye\s+alayim\s+mi\b",
            r"\bbugün\s+hava\b",
            r"\bbugun\s+hava\b",
            r"\byarın\s+hava\b",
            r"\byarin\s+hava\b",
            r"\bşu\s+an\b",
            r"\bsu\s+an\b",
        ]
        for pat in multi_word_patterns:
            working = re.sub(pat, " ", working, flags=re.IGNORECASE)

        single_stopwords = {
            "hava",
            "havadurumu",
            "durumu",
            "nasil",
            "nedir",
            "bugun",
            "yarin",
            "anlik",
            "acaba",
            "icin",
            "kac",
            "derece",
        }

        for word in working.split():
            # Strip Turkish locative/genitive suffixes like "Ankara'da", "İstanbul'un"
            base_word = re.sub(r"'(da|de|ta|te|dan|den|tan|ten|in|ın|un|ün|nin|nın|nun|nün)$", "", word, flags=re.IGNORECASE)
            norm_w = _normalize_tr(base_word)
            if not norm_w or norm_w in single_stopwords:
                continue
            norm_tokens.append(base_word)

        return " ".join(norm_tokens).strip()

    def resolve_location(self, location_query: str) -> ResolvedLocation:
        """Resolve a location string to coordinates using presets, Nominatim, or Open-Meteo."""
        if not location_query or not location_query.strip():
            return self.default_location

        norm = _normalize_tr(location_query)
        # Expand common neighborhood abbreviations like "mah" -> "mahallesi"
        norm_clean = re.sub(r"\bmah\.?\b", "", norm).strip()
        norm_clean = re.sub(r"\bmahallesi\b", "", norm_clean).strip()

        if norm_clean in PRESET_LOCATIONS:
            name, lat, lon = PRESET_LOCATIONS[norm_clean]
            return ResolvedLocation(display_name=name, latitude=lat, longitude=lon)

        if self._home_aliases and any(
            alias and alias in norm for alias in self._home_aliases
        ):
            return self.default_location

        # 1. Try OpenStreetMap Nominatim (great for neighborhood-level queries)
        try:
            expanded_query = re.sub(
                r"\bmah\.?\b", "Mahallesi", location_query, flags=re.IGNORECASE
            )
            params = urllib.parse.urlencode({
                "q": expanded_query,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
                "accept-language": "tr",
            })
            nom_url = f"https://nominatim.openstreetmap.org/search?{params}"
            nom_data = self._http_get_json(nom_url)
            if isinstance(nom_data, list) and len(nom_data) > 0:
                first = nom_data[0]
                lat = float(first["lat"])
                lon = float(first["lon"])
                display = first.get("display_name", location_query)
                # Keep display name concise (first 3 comma-separated parts)
                parts = [p.strip() for p in display.split(",") if p.strip()]
                short_display = ", ".join(parts[:3]) if parts else location_query
                return ResolvedLocation(
                    display_name=short_display,
                    latitude=lat,
                    longitude=lon,
                )
        except Exception:
            pass

        # 2. Fallback to Open-Meteo Geocoding API
        try:
            params = urllib.parse.urlencode({
                "name": location_query,
                "count": 1,
                "language": "tr",
                "format": "json",
            })
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{params}"
            geo_data = self._http_get_json(geo_url)
            if isinstance(geo_data, dict):
                results = geo_data.get("results") or []
                if results:
                    item = results[0]
                    name = item.get("name", location_query)
                    admin1 = item.get("admin1", "")
                    country = item.get("country", "")
                    label_parts = [p for p in (name, admin1, country) if p]
                    # Deduplicate consecutive identical names (e.g., "Ankara, Ankara")
                    deduped: list[str] = []
                    for p in label_parts:
                        if not deduped or deduped[-1].lower() != p.lower():
                            deduped.append(p)
                    return ResolvedLocation(
                        display_name=", ".join(deduped),
                        latitude=float(item["latitude"]),
                        longitude=float(item["longitude"]),
                    )
        except Exception:
            pass

        raise ValueError(
            f"'{location_query}' konumu bulunamadı. Lütfen geçerli bir şehir veya mahalle adı girin."
        )

    def fetch_weather(self, location_query: str = "") -> WeatherSnapshot:
        """Fetch current weather and 5-day forecast from Open-Meteo."""
        loc = self.resolve_location(location_query)

        params = urllib.parse.urlencode({
            "latitude": f"{loc.latitude:.4f}",
            "longitude": f"{loc.longitude:.4f}",
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 5,
        })
        url = f"https://api.open-meteo.com/v1/forecast?{params}"

        try:
            data = self._http_get_json(url)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Hava durumu servisine bağlanılamadı (İnternet bağlantınızı kontrol edin): {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Hava durumu verisi alınamadı: {exc}") from exc

        if not isinstance(data, dict) or "current" not in data:
            raise RuntimeError("Hava durumu API yanıtı geçersiz formatta.")

        current = data.get("current", {})
        daily = data.get("daily", {})

        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        t_max_list = daily.get("temperature_2m_max") or []
        t_min_list = daily.get("temperature_2m_min") or []
        precip_list = daily.get("precipitation_probability_max") or []

        forecast_items: list[DailyForecastItem] = []
        for idx, d_str in enumerate(dates):
            w_code = int(codes[idx]) if idx < len(codes) and codes[idx] is not None else 0
            tmax = float(t_max_list[idx]) if idx < len(t_max_list) and t_max_list[idx] is not None else 0.0
            tmin = float(t_min_list[idx]) if idx < len(t_min_list) and t_min_list[idx] is not None else 0.0
            prob = int(precip_list[idx]) if idx < len(precip_list) and precip_list[idx] is not None else 0
            forecast_items.append(
                DailyForecastItem(
                    date=d_str,
                    weather_code=w_code,
                    temp_min=tmin,
                    temp_max=tmax,
                    precip_prob=prob,
                )
            )

        return WeatherSnapshot(
            location=loc,
            temperature=float(current.get("temperature_2m", 0.0)),
            apparent_temperature=float(
                current.get("apparent_temperature", current.get("temperature_2m", 0.0))
            ),
            humidity=int(current.get("relative_humidity_2m", 0)),
            wind_speed=float(current.get("wind_speed_10m", 0.0)),
            weather_code=int(current.get("weather_code", 0)),
            daily=forecast_items,
        )

    def format_weather_markdown(self, snapshot: WeatherSnapshot) -> str:
        """Render a rich Markdown weather report suitable for ChatWindow."""
        icon, desc = describe_wmo_code(snapshot.weather_code)
        lines = [
            f"### {icon} {snapshot.location.display_name} — Hava Durumu",
            "",
            f"* **Anlık Durum:** {desc} (**{snapshot.temperature:.1f}°C** / Hissedilen: **{snapshot.apparent_temperature:.1f}°C**)",
            f"* **Nem:** %{snapshot.humidity} | **Rüzgar:** {snapshot.wind_speed:.1f} km/sa",
        ]

        if snapshot.daily:
            lines.append("")
            lines.append("#### 5 Günlük Tahmin")
            for day in snapshot.daily:
                d_icon, d_desc = describe_wmo_code(day.weather_code)
                day_label = _format_day_label(day.date)
                precip_str = f" — 💧 Yağış: %{day.precip_prob}" if day.precip_prob > 0 else ""
                lines.append(
                    f"* **{day_label}:** {d_icon} {d_desc}, "
                    f"**{day.temp_min:.0f}°C / {day.temp_max:.0f}°C**{precip_str}"
                )

        # Smart advice based on rain or cold temperature
        rain_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
        today_precip = snapshot.daily[0].precip_prob if snapshot.daily else 0
        if snapshot.weather_code in rain_codes or today_precip >= 40:
            lines.append("")
            lines.append(
                "> **JARVIS Önerisi:** Bölgede yağış beklentisi yüksek, dışarı çıkarken şemsiye bulundurman iyi olur."
            )
        elif snapshot.temperature <= 8.0:
            lines.append("")
            lines.append(
                "> **JARVIS Önerisi:** Hava oldukça serin, dışarı çıkarken kalın giyinmeni öneririm."
            )

        return "\n".join(lines)

    def get_weather_report(self, message: str = "") -> tuple[str, WeatherSnapshot]:
        """Extract location from user message, fetch weather, and return (markdown, snapshot)."""
        loc_query = self.extract_location_query(message)
        snapshot = self.fetch_weather(loc_query)
        return self.format_weather_markdown(snapshot), snapshot
