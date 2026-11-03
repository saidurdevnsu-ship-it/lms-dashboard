---
name: code-review
description: Use when reviewing STS Pulse changes or a diff. Checks invariants, permissions, MySQL safety, tests; read-only.
---

# Reviewing STS Pulse changes

Read first: `docs/architecture.md` (helpers, flows), `docs/database-design.md` (constraints, MySQL),
`docs/business-requirements.md` (roles, what's deferred). A review changes no files: report findings by severity
(file:line, problem, fix), then ask which to fix.

## Checklist

**Confidentiality and permissions**
- Final papers / final questions reachable only with `questionbank.manage_final_papers` or superuser (views,
  admin querysets, exports, `/media/` links, templates).
- Notifications, audit `details`, emails and Make payloads about final papers contain no questions.
- Permission edits keep `keep_final_papers_confidential`; roles still created only when missing (`ensure_roles`).
- New querysets in the Academic workspace go through `scope.in_scope` / `covers`.
- Any approve/reject path uses `learning.approvals.is_own` / `others_only` — superusers included.
- User/role admin can't grant more than the actor holds; staff pages with personal data are `never_cache`.

**Conventions**
- Business rules in service modules (`questionbank/workflow.py`, `hr/services.py`) raising
  `WorkflowError` / `GenerationError` / `LeaveError` with plain-language messages for school staff.
- Uses `audit.services.record`, `notifications.services.notify` / `users_with_perm`, `notifications.mail.send_email`,
  `notifications.make.send_event` — no ad-hoc email, HTTP or Anthropic clients.
- AI only via `ai._ask_claude` → `llm.ask` (no direct provider calls); long work via `threading.Thread` + `_in_thread` + `notify`.
- AI provider changes keep the guards: final-paper purposes refused on Gemini without `GEMINI_PAID_TIER`,
  `store=False` on every Gemini request, PDFs inline (no Files API), and no prompt/reply text in logs.
- Docstrings explain why. Workspace hiding still works for new admin apps/URLs (`learning/workspaces.py`).

**Database**
- Migration present and minimal; `makemigrations --check` clean. No conditional unique constraints without
  validation and the test allow-list. No reliance on `bulk_create` ids, case-sensitive matching or DB date
  functions. Signal receivers that write skip `raw=True`. Form limits come from model fields (strict mode).

**Tests and hygiene**
- New behaviour has tests that mock `_ask_claude` / `ai.threading.Thread` and use
  `@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")` where sending is possible.
- Both backends pass with the full test prefix incl. the blank `AI_*` / `GEMINI_*` values (`skills/testing/SKILL.md`).
- No secrets, `.env`, `db.sqlite3`, `backups/`, `media/`, `.idea/` in the diff. Commit message: one imperative line
  describing the user-visible change.
