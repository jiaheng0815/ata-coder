# Workflow Examples

Pre-built YAML workflow definitions for common development tasks.

## Usage

```bash
# In the ATA Coder REPL:
/run-workflow workflows/code-review.yaml

# Or trigger from the command line:
ata run-workflow workflows/ci-pipeline.yaml
```

## Included Workflows

| Workflow | Description |
|----------|-------------|
| `code-review.yaml` | Lint → multi-dimension review → summarize → optional auto-fix |
| `ci-pipeline.yaml` | Test (parallel unit + type check) → build → deploy → notify |

## Writing Your Own

Workflows are YAML files with this structure:

```yaml
name: my-workflow
description: What it does
vars:                          # optional defaults
  target: staging
steps:
  - id: step-1                 # unique step ID
    run: echo hello            # shell command

  - id: step-2
    tool: read_file            # any registered tool
    args:
      file_path: README.md

  - id: step-3
    agent: Review the code     # LLM-powered sub-task
    model: haiku               # optional model override
    depends_on: [step-1]       # wait for dependencies

  - id: step-4
    parallel:                  # run sub-steps concurrently
      - run: pytest tests/
      - agent: Check for security issues
```

## Step Types

- **`run`**: Shell command (`run_shell` tool)
- **`tool`**: Any registered tool by name, with `args` dict
- **`agent`**: LLM-powered sub-task with optional `model` override
- **`parallel`**: List of sub-steps that run concurrently

## Variables

Reference step outputs with `${{ step_id }}`:

```yaml
- id: build
  run: python -m build
- id: deploy
  run: twine upload ${{ build }}
```

## Conditions

Steps with `condition` only run when the expression is true:

```yaml
- id: deploy-prod
  run: ./deploy.sh production
  condition: "vars.target == production"
```
