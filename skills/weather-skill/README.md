# Weather Skill

Query real-time weather for cities worldwide using the free Open-Meteo API (no API key required).

## Usage

```python
from ata_coder.skills import get_skill_manager
mgr = get_skill_manager()
result = mgr.execute_skill("weather-skill", {
    "city": "Tokyo",
    "units": "celsius",
    "forecast_days": 3,
})
```

## Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| city | string | yes | — | City name |
| units | string | no | celsius | `celsius` or `fahrenheit` |
| forecast_days | integer | no | 1 | 1-7 days |

## Response Format

```json
{
  "success": true,
  "result": {
    "city": "Tokyo",
    "country": "Japan",
    "temperature": "22.5°C",
    "windspeed": "12 km/h",
    "conditions": "Clear sky",
    "humidity": "N/A (free API limitation)"
  }
}
```

## Installation

```bash
pip install -r requirements.txt
```
