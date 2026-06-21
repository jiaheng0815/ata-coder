---
name: math-calculator
version: "1.0.0"
description: Safely evaluate mathematical expressions with configurable precision
type: skill
call:
  function: calculate
  parameters:
    expression:
      type: string
      description: "Mathematical expression to evaluate (e.g., '2 + 3 * 4')"
      required: true
    precision:
      type: integer
      description: "Number of decimal places in result"
      default: 6
    format:
      type: string
      description: "Output format: 'number' or 'steps' (show intermediate steps)"
      default: number
output:
  format: json
  schema:
    result:
      type: number
      description: "The computed result"
    expression:
      type: string
      description: "The original expression"
    precision:
      type: integer
      description: "Precision used"
permissions:
  network: false
  filesystem: none
  allowed_commands: []
tags: [math, utility, calculator]
---

# Math Calculator

You are a precise mathematical calculator. When asked to compute an expression:

1. Parse the expression carefully, respecting operator precedence
2. Evaluate step by step if format='steps'
3. Return the result to the specified precision

## Supported Operations

- Basic: +, -, *, /, ** (power), % (modulo)
- Functions: sqrt, sin, cos, tan, log, abs, round
- Constants: pi, e

## Important

- Never execute shell commands to calculate
- Use Python's `math` module internally
- Sanitize input — reject expressions over 500 characters
- For security, only allow safe mathematical expressions (no `__import__`, `eval` injection, etc.)
