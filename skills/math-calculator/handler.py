# -*- coding: utf-8 -*-
"""Math Calculator Skill — safe expression evaluation using AST validation."""

import ast
import math
import operator
from typing import Any


# ── Allowed operators (whitelist) ────────────────────────────────────────
_SAFE_OPERATORS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# Allowed built-in functions (name → callable)
_SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs, "round": round, "int": int, "float": float,
    "max": max, "min": min, "sum": sum, "pow": pow, "len": len,
    "range": range, "list": list, "tuple": tuple,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "log": math.log, "log10": math.log10,
    "log2": math.log2, "exp": math.exp,
    "ceil": math.ceil, "floor": math.floor,
    "degrees": math.degrees, "radians": math.radians,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "fabs": math.fabs, "factorial": math.factorial,
    "gcd": math.gcd, "trunc": math.trunc, "modf": math.modf,
    "isclose": math.isclose,
}

# Allowed constants
_SAFE_CONSTANTS: dict[str, Any] = {
    "pi": math.pi, "e": math.e, "True": True, "False": False, "None": None,
}


class _SafeEvaluator(ast.NodeVisitor):
    """AST-based safe expression evaluator — no eval(), no code injection."""

    def __init__(self):
        self._stack: list[Any] = []

    def push(self, value: Any) -> None:
        self._stack.append(value)

    def pop(self) -> Any:
        return self._stack.pop()

    def visit_Expression(self, node: ast.Expression) -> Any:
        # Use super().generic_visit to traverse children (our override
        # raises on unexpected nodes, but Expression children are expected).
        super(_SafeEvaluator, self).generic_visit(node)
        return self._stack[-1] if self._stack else None

    def visit_Constant(self, node: ast.Constant) -> None:
        self.push(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _SAFE_CONSTANTS:
            self.push(_SAFE_CONSTANTS[node.id])
        else:
            raise ValueError(f"Unknown name: {node.id}")

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function calls are allowed")
        func_name = node.func.id
        if func_name not in _SAFE_FUNCTIONS:
            raise ValueError(f"Function not allowed: {func_name}")
        args = []
        for arg in node.args:
            self.visit(arg)
            args.append(self.pop())
        kwargs = {}
        for kw in node.keywords:
            self.visit(kw.value)
            kwargs[kw.arg] = self.pop()
        result = _SAFE_FUNCTIONS[func_name](*args, **kwargs)
        self.push(result)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.visit(node.operand)
        operand = self.pop()
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unary operator not allowed: {op_type.__name__}")
        self.push(_SAFE_OPERATORS[op_type](operand))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.visit(node.left)
        left = self.pop()
        self.visit(node.right)
        right = self.pop()
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Binary operator not allowed: {op_type.__name__}")
        self.push(_SAFE_OPERATORS[op_type](left, right))

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise ValueError("Only single comparisons are allowed")
        self.visit(node.left)
        left = self.pop()
        self.visit(node.comparators[0])
        right = self.pop()
        op_type = type(node.ops[0])
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Comparison operator not allowed: {op_type.__name__}")
        self.push(_SAFE_OPERATORS[op_type](left, right))

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if len(node.values) != 2:
            raise ValueError("Only two-operand boolean expressions are allowed")
        self.visit(node.values[0])
        left = self.pop()
        self.visit(node.values[1])
        right = self.pop()
        if isinstance(node.op, ast.And):
            self.push(left and right)
        elif isinstance(node.op, ast.Or):
            self.push(left or right)
        else:
            raise ValueError(f"Boolean operator not allowed: {type(node.op).__name__}")

    # ── Explicitly deny everything else ──────────────────────────────────
    def generic_visit(self, node: ast.AST) -> None:
        raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def run(input_data: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate a mathematical expression safely using AST-based validation.

    Args:
        input_data: {"expression": str, "precision": int = 6, "format": str = "number"}

    Returns:
        {"success": bool, "result": number|null, "expression": str, "error": str|null}
    """
    expression = input_data.get("expression", "").strip()
    precision = input_data.get("precision", 6)
    input_data.get("format", "number")

    # Validate
    if not expression:
        return _error("No expression provided", expression, 400)

    if len(expression) > 500:
        return _error("Expression too long (>500 chars)", expression, 400)

    # Parse AST — this rejects all code injection (imports, exec, etc.)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return _error(f"Syntax error: {e}", expression, 400)

    # Evaluate safely via AST visitor (no eval(), no code execution)
    evaluator = _SafeEvaluator()
    try:
        result = evaluator.visit(tree)
    except ValueError as e:
        return _error(str(e), expression, 403)
    except ZeroDivisionError:
        return _error("Division by zero", expression, 400)
    except OverflowError as e:
        return _error(f"Numerical overflow: {e}", expression, 400)
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
