"""Open-Meteo weather fetcher with server-side caching.

No API key required. Two HTTP calls:
  1. Geocoding API  -> lat/lon from a location string
  2. Forecast API   -> current conditions + 7-day daily forecast

Results are cached for WEATHER_CACHE_TTL seconds (default 10 min) so the
browser's 3-second dashboard poll never hammers the upstream API.
"""
import time
import threading
import urllib.parse
import urllib.request
import json
from typing import Optional

import config

# WMO weather code -> (description, emoji)
WMO_CODES = {
    0:  ("Clear sky",          "☀️"),
    1:  ("Mainly clear",       "🌤️"),
    2:  ("Partly cloudy",      "⛅"),
    3:  ("Overcast",           "☁️"),
    45: ("Foggy",              "🌫️"),
    48: ("Icy fog",            "🌫️"),
    51: ("Light drizzle",      "🌦️"),
    53: ("Drizzle",            "🌦️"),
    55: ("Heavy drizzle",      "🌦️"),
    61: ("Light rain",         "🌧️"),
    63: ("Rain",               "🌧️"),
    65: ("Heavy rain",         "🌧️"),
    71: ("Light snow",         "🌨️"),
    73: ("Snow",               "🌨️"),
    75: ("Heavy snow",         "❄️"),
    77: ("Snow grains",        "❄️"),
    80: ("Light showers",      "🌦️"),
    81: ("Showers",            "🌦️"),
    82: ("Heavy showers",      "🌦️"),
    85: ("Snow showers",       "🌨️"),
    86: ("Heavy snow showers", "🌨️"),
    95: ("Thunderstorm",       "⛈️"),
    96: ("Thunderstorm + hail","⛈️"),
    99: ("Thunderstorm + hail","⛈️"),
}

COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
           "S","SSW","SW","WSW","W","WNW","NW","NNW"]

def _wind_dir(degrees: float) -> str:
    return COMPASS[round(degrees / 22.5) % 16]

def _wmo(code: int) -> tuple:
    return WMO_CODES.get(code, ("Unknown", "❓"))

def _c_to_f(c: float) -> float:
    return round(c * 9/5 + 32, 1)

def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=config.QRZ_TIMEOUT) as r:
        return json.loads(r.read())

def _geocode(location: str) -> Optional[tuple]:
    """Return (lat, lon, display_name) or None."""
    params = urllib.parse.urlencode({"name": location, "count": 1, "format": "json"})
    data   = _fetch_json(f"{config.GEOCODE_URL}?{params}")
    if not data.get("results"):
        return None
    r = data["results"][0]
    name = r.get("name", "")
    state = r.get("admin1", "")
    country = r.get("country_code", "")
    display = ", ".join(p for p in [name, state] if p) or country
    return r["latitude"], r["longitude"], display


class WeatherClient:
    def __init__(self):
        self._lock       = threading.Lock()
        self._cache: dict = {}          # location_key -> (expiry, result)
        self._geo_cache: dict = {}      # location_str  -> (lat, lon, display)

    def get(self, location: str, unit: str = "F") -> Optional[dict]:
        """Return weather dict or None if location blank or fetch fails."""
        location = location.strip()
        if not location:
            return None

        cache_key = f"{location}|{unit}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > time.time():
                return cached[1]

        result = self._fetch(location, unit)
        with self._lock:
            self._cache[cache_key] = (time.time() + config.WEATHER_CACHE_TTL, result)
        return result

    def _fetch(self, location: str, unit: str) -> Optional[dict]:
        try:
            # Geocode (cached separately so unit changes don't re-geocode)
            with self._lock:
                geo = self._geo_cache.get(location)
            if not geo:
                geo = _geocode(location)
                if not geo:
                    return None
                with self._lock:
                    self._geo_cache[location] = geo
            lat, lon, display_name = geo

            temp_unit   = "fahrenheit" if unit == "F" else "celsius"
            wind_unit   = "mph"        if unit == "F" else "kmh"
            symbol      = "°F"         if unit == "F" else "°C"

            params = urllib.parse.urlencode({
                "latitude":   lat,
                "longitude":  lon,
                "current":    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                              "weather_code,wind_speed_10m,wind_direction_10m,visibility",
                "daily":      "weather_code,temperature_2m_max,temperature_2m_min",
                "temperature_unit": temp_unit,
                "wind_speed_unit":  wind_unit,
                "forecast_days":    7,
                "timezone":         "auto",
            })
            data    = _fetch_json(f"{config.OPEN_METEO_URL}?{params}")
            current = data["current"]
            daily   = data["daily"]

            wmo_desc, wmo_icon = _wmo(current["weather_code"])

            def fmt_temp(v):
                return f"{round(v)}{symbol}"

            days = []
            day_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
            for i, date_str in enumerate(daily["time"]):
                import datetime
                d = datetime.date.fromisoformat(date_str)
                desc, icon = _wmo(daily["weather_code"][i])
                days.append({
                    "name":  "Today" if i == 0 else day_names[d.weekday()] if hasattr(d, 'weekday') else day_names[(d.isoweekday() % 7)],
                    "icon":  icon,
                    "hi":    fmt_temp(daily["temperature_2m_max"][i]),
                    "lo":    fmt_temp(daily["temperature_2m_min"][i]),
                    "today": i == 0,
                })

            vis_raw = current.get("visibility", 0)
            vis     = f"{round(vis_raw / 1609):.0f} mi" if unit == "F" else f"{round(vis_raw / 1000):.0f} km"
            wind_spd = f"{round(current['wind_speed_10m'])} {'mph' if unit == 'F' else 'km/h'}"
            wind_dir = _wind_dir(current["wind_direction_10m"])

            return {
                "location":    display_name,
                "temp":        fmt_temp(current["temperature_2m"]),
                "feels_like":  fmt_temp(current["apparent_temperature"]),
                "description": wmo_desc,
                "icon":        wmo_icon,
                "humidity":    f"{current['relative_humidity_2m']}%",
                "wind":        f"{wind_spd} {wind_dir}",
                "visibility":  vis,
                "days":        days,
            }
        except Exception:
            return None
