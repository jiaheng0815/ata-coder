"""Weather Skill — utility functions (local to this skill)."""

import json
from pathlib import Path
from typing import Any

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → human-readable
WMO_CODES: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def load_city_coords() -> dict[str, dict]:
    """Load city coordinates from local resource file."""
    resource_path = Path(__file__).parent / "resources" / "city_list.json"
    if resource_path.exists():
        with open(resource_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def format_weather_response(city: str, country: str, api_data: dict,
                            units: str) -> dict[str, Any]:
    """Format API response into a clean result."""
    current = api_data.get("current_weather", {})
    code = current.get("weathercode", 0)
    temp = current.get("temperature", "N/A")
    unit_symbol = "°C" if units == "celsius" else "°F"

    return {
        "success": True,
        "result": {
            "city": city.title(),
            "country": country,
            "temperature": f"{temp}{unit_symbol}",
            "windspeed": f"{current.get('windspeed', 'N/A')} km/h",
            "conditions": WMO_CODES.get(code, f"Unknown ({code})"),
            "humidity": "N/A (free API limitation)",
        },
        "error": None,
    }
