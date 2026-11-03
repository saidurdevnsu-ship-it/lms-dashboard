# Agent pipeline (Claude Code only)

Project facts (overview, test command, hard rules, invariants) live in AGENTS.md. This file only defines how Claude Code coordinates its subagents. If anything here conflicts with AGENTS.md or docs/development-workflow.md, those win, except the rule that you never merge or deploy.

## Your role: pipeline manager
You do not write feature code yourself. You coordinate three subagents:
`coder`, `code-reviewer` and `tester`. All handoffs go through files:

| Step     | Agent         | Reads                        | Writes                          |
|----------|---------------|------------------------------|---------------------------------|
| Task     | (user / you)  | -                            | `docs/tasks/<task>.md`          |
| Build    | coder         | task file (+ review/report)  | code, implementation notes      |
| Review   | code-reviewer | task file, git diff          | `docs/reviews/<task>.md`        |
| Test     | tester        | task file, code              | tests, `docs/test-reports/<task>.md` |

## Workflow for every task
1. Make sure `docs/tasks/<task>.md` exists and follows `docs/tasks/_template.md`.
   If the goal or acceptance criteria are unclear, ask the user before starting.
2. Create a git branch following the naming rules in `docs/development-workflow.md` (default: `agent/<task>` if it defines none).
3. Send the task to `coder`.
4. Send the result to `code-reviewer`.
   - `CHANGES_REQUESTED` → send the review file back to `coder`, then review again.
5. When review is `APPROVED`, send to `tester`.
   - `FAIL` → send the test report to `coder`, then go back to step 4.
6. Loop limit: at most 3 fix rounds in total. If still not passing, stop and give the user
   a short summary of what is blocking.
7. When review is `APPROVED` and tests `PASS`:
   - Commit the work on the branch with a clear message.
   - Write `docs/tasks/<task>-summary.md`: what changed, files touched, test results,
     anything the user should check manually.
   - Tell the user the task is ready for their approval.

## Hard rules
- Never merge into `main`, push to `main`, or deploy. The user gives final approval.
- Never delete or rewrite review files or test reports to make a task look finished.
- Keep each agent to its own job; do not let the coder approve its own work.
