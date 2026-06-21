"""Tests for weather-skill handler (unit tests, no network)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from handler import run


def test_missing_city():
    result = run({"city": ""})
    assert not result["success"]
    assert "City name required" in result["error"]


def test_unknown_city_no_network():
    """Without httpx installed, unknown cities should fail gracefully."""
    result = run({"city": "asdfghjkl"})
    assert not result["success"]


def test_known_city_no_httpx():
    """With city_list.json but no httpx, should report httpx missing."""
    result = run({"city": "Beijing"})
    # Either finds coords in city_list but fails on httpx,
    # or succeeds if httpx is installed
    if not result["success"]:
        assert "httpx" in result.get("error", "").lower() or "city" in result.get("error", "").lower()
