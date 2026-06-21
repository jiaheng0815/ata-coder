## Slash Commands

### /security-review
Perform a focused security review of changes on the current branch. Only flag issues where you are >80% confident. Prioritize vulnerabilities that could lead to unauthorized access, data breaches, or system compromise. Hard exclusions: DoS, rate limiting, memory safety in safe languages, unit tests only.

### /batch
Orchestrate parallel work:
1. **Research and Plan** (Plan mode): launch Explore agents, decompose into 5-30 independent units, determine end-to-end test recipe, write plan, call `ExitPlanMode`.
2. **Spawn Workers**: one background agent per unit using `isolation: "worktree"` and `run_in_background: true`. Launch all in a single message block.
3. **Track Progress**: render status table and update as agents complete.

### /review-pr
Review a GitHub pull request using `gh` CLI; fetch PR details and provide structured code review feedback.

### /pr-comments
Fetch and display comments from a GitHub PR using `gh pr view --json` and `gh api`.

### Git Commit Workflow
1. Run `git status`, `git diff`, `git log` in parallel
2. Analyze all staged changes, draft commit message
3. Add files, create commit, run `git status` in parallel
4. If pre-commit hook fails, retry ONCE

NEVER update git config. NEVER use `-i` flag. Always pass message via HEREDOC.
