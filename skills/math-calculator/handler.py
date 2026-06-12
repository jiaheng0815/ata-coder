# -*- coding: utf-8 -*-
"""Math Calculator Skill — safe expression evaluation."""

import math
import re
from typing import Any


# Allowed builtins for safe evaluation
_SAFE_BUILTINS = {
    "abs": abs, "round": round, "int": int, "float": float,
    "max": max, "min": min, "sum": sum, "pow": pow,
    "len": len, "range": range, "list": list, "tuple": tuple,
}
_SAFE_MATH = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "log": math.log, "log10": math.log10,
    "log2": math.log2, "exp": math.exp, "pi": math.pi, "e": math.e,
    "ceil": math.ceil, "floor": math.floor, "degrees": math.degrees,
    "radians": math.radians, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "fabs": math.fabs, "factorial": math.factorial,
    "gcd": math.gcd, "trunc": math.trunc, "modf": math.modf,
    "isclose": math.isclose,
}
_SAFE_NAMES = {**_SAFE_BUILTINS, **_SAFE_MATH}

# Regex for sanitizing expressions
_UNSAFE_PATTERNS = [
    r"__",           # dunder attributes
    r"import",       # imports
    r"exec\s*\(",    # exec()
    r"eval\s*\(",    # recursive eval
    r"open\s*\(",    # file open
    r"os\.",         # os module
    r"sys\.",        # sys module
    r"subprocess",   # subprocess
    r"lambda",       # lambda (can create arbitrary code)
    r"globals?\s*\(",  # globals()
    r"locals?\s*\(",   # locals()
    r"getattr\s*\(",   # getattr()
    r"setattr\s*\(",   # setattr()
]


def run(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate a mathematical expression safely.

    Args:
        input_data: {"expression": str, "precision": int = 6, "format": str = "number"}

    Returns:
        {"success": bool, "result": number|null, "expression": str, "error": str|null}
    """
    expression = input_data.get("expression", "").strip()
    precision = input_data.get("precision", 6)
    output_format = input_data.get("format", "number")

    # Validate
    if not expression:
        return _error("No expression provided", expression, 400)

    if len(expression) > 500:
        return _error("Expression too long (>500 chars)", expression, 400)

    # Sanitize — check for unsafe patterns
    lowered = expression.lower()
    for pattern in _UNSAFE_PATTERNS:
        if re.search(pattern, lowered):
            return _error(f"Unsafe pattern detected: {pattern}", expression, 403)

    # Evaluate safely
    try:
        result = eval(expression, {"__builtins__": {}}, _SAFE_NAMES)
    except SyntaxError as e:
        return _error(f"Syntax error: {e}", expression, 400)
    except ZeroDivisionError:
        return _error("Division by zero", expression, 400)
    except Exception as e:
        return _error(f"Evaluation error: {type(e).__name__}: {e}", expression, 400)

    # Format
    if isinstance(result, float):
        result = round(result, precision)

    return {
        "success": True,
        "result": result,
        "expression": expression,
        "precision": precision,
        "error": None,
    }


def _error(msg: str, expr: str, code: int) -> dict[str, Any]:
    return {
        "success": False,
        "result": None,
        "expression": expr,
        "error": msg,
        "status_code": code,
    }
