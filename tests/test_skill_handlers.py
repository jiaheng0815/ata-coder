"""Tests for skill handlers — loaded dynamically from skills/ folders."""

import sys
from pathlib import Path


SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _load_handler(skill_name: str):
    """Dynamically load a skill's handler.py (with its dir in sys.path)."""
    skill_dir = SKILLS_DIR / skill_name
    handler_path = skill_dir / "handler.py"
    if not handler_path.exists():
        return None
    # Add skill dir to path so handler can import utils.py etc.
    old_path = list(sys.path)
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"test_{skill_name}", str(handler_path)
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = old_path


# ── Math Calculator ─────────────────────────────────────────────────────

class TestMathCalculator:
    def setup_method(self):
        self.mod = _load_handler("math-calculator")
        assert self.mod is not None, "math-calculator handler not found"

    def test_basic(self):
        r = self.mod.run({"expression": "2 + 3"})
        assert r["success"]
        assert r["result"] == 5

    def test_precedence(self):
        r = self.mod.run({"expression": "10 - 3 * 2"})
        assert r["success"]
        assert r["result"] == 4

    def test_sqrt(self):
        r = self.mod.run({"expression": "sqrt(144) + 1"})
        assert r["success"]
        assert r["result"] == 13.0

    def test_trig(self):
        r = self.mod.run({"expression": "sin(0)"})
        assert r["success"]
        assert r["result"] == 0.0

    def test_pi(self):
        r = self.mod.run({"expression": "round(pi, 3)"})
        assert r["success"]
        assert result_ok(r["result"], 3.142)

    def test_empty_rejected(self):
        r = self.mod.run({"expression": ""})
        assert not r["success"]
        assert r["status_code"] == 400

    def test_unsafe_import_rejected(self):
        r = self.mod.run({"expression": "__import__('os').listdir()"})
        assert not r["success"]

    def test_unsafe_eval_rejected(self):
        r = self.mod.run({"expression": "eval('1+1')"})
        assert not r["success"]

    def test_unsafe_open_rejected(self):
        r = self.mod.run({"expression": "open('/etc/passwd')"})
        assert not r["success"]

    def test_div_zero(self):
        r = self.mod.run({"expression": "1/0"})
        assert not r["success"]


# ── Weather ──────────────────────────────────────────────────────────────

class TestWeatherSkill:
    def setup_method(self):
        self.mod = _load_handler("weather-skill")
        assert self.mod is not None, "weather-skill handler not found"

    def test_missing_city(self):
        r = self.mod.run({"city": ""})
        assert not r["success"]
        assert "required" in r["error"].lower() or "city" in r["error"].lower()

    def test_known_city_no_httpx(self):
        """Should gracefully handle missing httpx."""
        r = self.mod.run({"city": "Beijing"})
        if not r["success"]:
            # Either httpx not installed, or API call works
            assert "httpx" in r.get("error", "").lower() or "error" in r

    def test_unknown_city(self):
        r = self.mod.run({"city": "asdfghjkl999"})
        if not r["success"]:
            assert r["error"] is not None


# ── Helpers ──────────────────────────────────────────────────────────────

def result_ok(actual, expected):
    """Compare numeric results with tolerance."""
    return abs(actual - expected) < 0.001
