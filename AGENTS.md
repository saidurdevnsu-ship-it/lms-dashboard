# AGENTS.md

"STS Pulse": Django 6.1 school LMS + HR system for STS Group (Windows 11, Python 3.15 venv at `venv/`). The UI is
Django admin only (`templates/admin/`, `static/css/admin-*.css`). Apps: `learning` (sign-in, Academic/HR
workspaces, roles = Django Groups, profile), `questionbank` (Academic: materials, Claude question generation,
blueprints, confidential final papers, exports), `hr`, `notifications` (in-app, email, Make.com), `audit`
(read-only log). Time zone `Asia/Dhaka`; content in English or Bangla. Runs on MySQL 8.4 when `DB_NAME` is set,
else SQLite. Terminal is git-bash: native tools need `C:/...` paths, not `/tmp`.

## Test (always with blanked integrations — `.env` holds live webhook and API values)

```bash
env MAKE_WEBHOOK_URL= MAKE_MAILHOOK_ADDRESS= ANTHROPIC_API_KEY= EMAIL_HOST= venv/Scripts/python.exe manage.py test --noinput
# add DB_NAME= after `env` for SQLite; run both backends before calling work done
```

## Hard rules

- **Ask first** for: `git commit`, `git push` (separate yes), `migrate` on the real DB (back up to `backups/`
  first), package installs, paid Claude calls (`check_claude`), real Make sends (`send_make_test`).
- **Never print** `.env` values or MySQL credentials (`C:\Users\User\AppData\Local\MySQL-dev\`).
- **Final papers are confidential**: only `questionbank.manage_final_papers` (Exam Controller role) and superusers
  see them. Keep `keep_final_papers_confidential`; notifications/audit/emails never include final-paper questions.
- **Roles** are created only when missing (post_migrate), so admin edits persist; reset only on request.
- **No self-approval**: nobody, superusers included, approves own question/blueprint/leave
  (`learning.approvals.is_own` / `others_only`).
- **Subject scope**: Academic querysets go through `questionbank.scope.in_scope` / `covers`.
- **Use the helpers**: `audit.services.record`, `notifications.services.notify` / `users_with_perm`,
  `notifications.mail.send_email`, `notifications.make.send_event`. Claude only via `questionbank.ai._ask_claude`;
  slow work in `threading.Thread` via `_in_thread`, then `notify`.
- **Business rules** live in services (`questionbank/workflow.py`, `hr/services.py`) raising `WorkflowError` /
  `GenerationError` / `LeaveError` with plain-language messages for school staff. Docstrings explain *why*.
- **MySQL-safe**: no conditional unique constraints without validation; case-insensitive collation; `bulk_create`
  returns no ids; signal receivers that write skip `raw=True`.
- **Tests** mock `ai._ask_claude`, patch `ai.threading.Thread`, and use
  `@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")` where sending is possible.
- **Commits**: one short imperative line describing the user-visible change, optional bullet body.

## Read before the task

| Task | Read |
|---|---|
| Any code change | `docs/architecture.md`, `skills/django-development/SKILL.md` |
| Models, migrations, MySQL, backups | `docs/database-design.md` |
| Writing or running tests | `skills/testing/SKILL.md`, `docs/development-workflow.md` |
| Reviewing a diff | `skills/code-review/SKILL.md` |
| Scope, roles, what's built or deferred | `docs/business-requirements.md`, local plans in `.hermes/plans/` (not committed) |
| Setup, commands, commit/approval rules, Windows pitfalls | `docs/development-workflow.md` |

The `skills/` files are plain guidance (not auto-loaded); open them when the table says so.
