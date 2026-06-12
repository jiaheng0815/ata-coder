# Math Calculator Skill

Safely evaluate mathematical expressions with configurable precision.

## Quick Start

```python
from ata_coder.skills import get_skill_manager

mgr = get_skill_manager()
result = mgr.execute_skill("math-calculator", {
    "expression": "2 + 3 * 4",
    "precision": 2,
})
print(result)  # {"success": True, "result": 14, ...}
```

## Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| expression | string | yes | — | Math expression |
| precision | integer | no | 6 | Decimal places |
| format | string | no | number | `number` or `steps` |

## Supported Operations

- Arithmetic: `+ - * / ** %`
- Functions: `sqrt sin cos tan log abs round`
- Constants: `pi e`

## Security

Input is sanitized against code injection. Expressions over 500 chars are rejected.

## Testing

```bash
pytest tests/
```
