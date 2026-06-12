"""Weather Skill — query weather via Open-Meteo API (free, no API key)."""

import json
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

from utils import load_city_coords, format_weather_response, GEO_URL, WEATHER_URL


def run(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Get weather for a city.

    Args:
        input_data: {"city": str, "units": str = "celsius", "forecast_days": int = 1}

    Returns:
        {"success": bool, "result": dict|null, "error": str|null}
    """
    city = input_data.get("city", "").strip()
    units = input_data.get("units", "celsius")
    forecast_days = min(max(int(input_data.get("forecast_days", 1)), 1), 7)

    if not city:
        return {"success": False, "result": None, "error": "City name required"}

    # Look up coordinates from local resource
    cities = load_city_coords()
    coords = cities.get(city.lower())
    if not coords:
        # Try geocoding API
        coords = _geocode_city(city)
        if not coords:
            return {
                "success": False, "result": None,
                "error": f"City not found: {city}",
            }

    lat, lon = coords["lat"], coords["lon"]

    # Fetch weather
    if httpx is None:
        return {"success": False, "result": None, "error": "httpx not installed"}
    try:
        params = {
            "latitude": lat, "longitude": lon,
            "current_weather": "true",
            "forecast_days": forecast_days,
            "temperature_unit": "celsius" if units == "celsius" else "fahrenheit",
        }
        resp = httpx.get(WEATHER_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"success": False, "result": None, "error": f"API error: {e}"}

    return format_weather_response(city, coords.get("country", ""), data, units)


def _geocode_city(city: str) -> dict | None:
    """Fallback: geocode city name via Open-Meteo Geocoding API."""
    if httpx is None:
        return None
    try:
        resp = httpx.get(GEO_URL, params={"name": city, "count": 1}, timeout=5.0)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            r = results[0]
            return {"lat": r["latitude"], "lon": r["longitude"], "country": r.get("country", "")}
    except Exception:
        pass
    return None
