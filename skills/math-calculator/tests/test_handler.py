# -*- coding: utf-8 -*-
"""Tests for math-calculator handler."""

import sys
from pathlib import Path

# Add parent to path so we can import handler
sys.path.insert(0, str(Path(__file__).parent.parent))
from handler import run


def test_basic_addition():
    result = run({"expression": "2 + 3"})
    assert result["success"]
    assert result["result"] == 5


def test_precedence():
    result = run({"expression": "2 + 3 * 4"})
    assert result["success"]
    assert result["result"] == 14


def test_sqrt():
    result = run({"expression": "sqrt(16)"})
    assert result["success"]
    assert result["result"] == 4.0


def test_empty_expression():
    result = run({"expression": ""})
    assert not result["success"]
    assert result["status_code"] == 400


def test_unsafe_import():
    result = run({"expression": "__import__('os')"})
    assert not result["success"]


def test_unsafe_eval():
    result = run({"expression": "eval('1+1')"})
    assert not result["success"]


def test_division_by_zero():
    result = run({"expression": "1/0"})
    assert not result["success"]


def test_long_expression():
    result = run({"expression": "1+" * 300})
    assert not result["success"]
