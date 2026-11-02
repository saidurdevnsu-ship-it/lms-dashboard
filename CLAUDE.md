# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"STS Pulse": a Django 6.1 school LMS + HR system for STS Group. There is no separate frontend. Everything runs inside
**Django admin**, which is customized with overridden templates in `templates/admin/`, custom admin views (via
`get_urls`) and the CSS in `static/css/admin-*.css`. Time zone is `Asia/Dhaka`, and content can be English or Bangla.

## Commands

Windows, with a venv at `venv/` (use `venv/Scripts/python`, or activate it first):

```bash
pip install -r requirements.txt
python manage.py migrate                 # also creates any missing predefined roles (post_migrate hook)
python manage.py runserver
python manage.py test                    # all tests (SQLite, or MySQL when DB_NAME is set)
python manage.py test questionbank       # one app
python manage.py test questionbank.tests.SomeTestCase.test_method   # one test
python manage.py makemigrations
```

Project management commands:
- `setup_academics [--reset]` / `setup_hr [--reset]`: create the predefined roles (and the default leave types for HR). `--reset` restores the default permissions of roles that already exist.
- `send_reminders`: creates today's deadline reminders and emails the new ones (meant to run on a schedule).
- `check_claude`: sends one tiny request to check the Anthropic API key and model.
- `send_make_test [events...]`: sends sample payloads to the Make.com webhook or mailhook.

There is no linter or formatter configured.

## Configuration

`config/settings.py` reads a git-ignored `.env` with its own small loader; real environment variables win.
`stslms.env` is the committed template. `DJANGO_SECRET_KEY` is required, and startup fails without it. Settings
switch on what is set:
- `DB_NAME` set → MySQL (utf8mb4, read committed); otherwise `db.sqlite3`. MySQL has no conditional unique constraints, so the one on `SubjectAssignment` is also enforced in admin form validation.
  `mysqlclient` is in `requirements.txt`, so a Linux server needs the MySQL client headers and a compiler to install it (e.g. `default-libmysqlclient-dev build-essential pkg-config` on Debian/Ubuntu), even if it stays on SQLite.
  `models.W036` is silenced for MySQL; `questionbank.tests` lists the conditional constraints allowed to exist, so a new one fails a test until it's also enforced by validation.
  MySQL needs its time zone tables loaded, or date-based admin pages (the audit log's date filter) fail with "invalid datetime value". Linux: `mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root mysql`. Windows: load `timezone_posix.sql` from https://dev.mysql.com/downloads/timezones.html into the `mysql` schema. Check: `SELECT CONVERT_TZ(NOW(), 'UTC', 'Asia/Dhaka')` isn't NULL.
  `bulk_create` doesn't return new ids on MySQL. Don't use the objects it returns for their `pk`; save one by one (or re-query) when callers need ids.
- `EMAIL_HOST` set → SMTP; otherwise emails print to the console. This uses Django 6's `MAILERS` setting, not `EMAIL_BACKEND`.
- `DJANGO_DEBUG=true` for local development. `SITE_URL` sets the absolute links in emails, `ALLOWED_HOSTS` and CSRF origins.
- `ANTHROPIC_API_KEY` and `QUESTION_GENERATION_MODEL` (default `claude-opus-5-5`) for AI generation. `MAKE_WEBHOOK_URL` / `MAKE_MAILHOOK_ADDRESS` for Make.com events.

## Architecture

Apps: `learning` (sign-in, workspaces, roles, users, profile), `questionbank` (academics), `hr`, `notifications`,
`audit`.

### Two workspaces, one sign-in (`learning/workspaces.py`)
After sign-in, users pick **Academic** (`questionbank`) or **HR** (`hr` + `auth`) and see only that workspace. The
choice is stored in the session. `LearningConfig.ready()` monkey-patches `admin.site.get_app_list` to hide the other
workspace's apps. `WorkspaceMiddleware` switches workspace when someone opens a URL that belongs to the other one.
Notifications that link into a hidden workspace are filtered out with `without_hidden_links`. `/admin/` is routed to
`learning.views.dashboard` first, which sends the user to the picker or the HR home. Django admin's own login
redirects to the shared `/accounts/login/`.

### Roles = Django Groups (`learning/roles.py`)
Groups are shown as "roles". `DEFAULT_ROLES` defines the predefined roles and their permissions (`app.*` wildcards,
and a `-` prefix to exclude). Roles are created only when missing, so edits made by admins persist; they are reset
only on request. `keep_final_papers_confidential` strips `questionbank.manage_final_papers` from every role except
Exam Controller. Keep that invariant when you touch permissions.

### Cross-cutting rules
- **Subject scope** (`questionbank/scope.py`): non-superusers see only the subjects and grades in their `SubjectAssignment`s (a null grade means all grades), and nothing at all without one. Superusers and holders of `manage_final_papers` see everything. Admin querysets go through `in_scope(qs, user, prefix)`.
- **No self-approval** (`learning/approvals.py`): nobody, superusers included, approves their own question, blueprint or leave request. Use `is_own` / `others_only` in any new review flow.
- **Audit** (`audit/`): call `audit.services.record(actor, Action.X, target, ...)` for significant actions. Admin add/change/delete is logged automatically through a `LogEntry` post_save signal, and sign-ins through auth signals. The audit log is read-only.
- **Notifications** (`notifications/services.notify`): in-app notifications, optionally deduplicated with `dedupe_key`. Find recipients with `users_with_perm`. `questionbank.reminders.ensure_reminders` also runs lazily from the notifications context processor on page loads.
- **Email** goes through `notifications.mail.send_email` (absolute links via `site_url`). **Make.com** events go through `notifications.make.send_event`, which sends from a thread after `transaction.on_commit`.

### Question bank flow (`questionbank/`)
- `KnowledgeSource`: uploaded books and materials (PDF/Word). Their text is extracted by `documents.py` (pymupdf, python-docx).
- `ai.py`: generates questions with Claude from the chosen sources, using structured JSON output (`_ask_claude` is the single API call point, and tests mock it). `checks.py` flags duplicates and questions copied from samples.
- `workflow.py`: blueprint state machine, Teacher/SME → HOD review → Exam Controller → confidential `FinalPaper`. Final papers are written in a background `threading.Thread` (`ai.start_final_paper`) because generation takes minutes. The thread closes its DB connection and notifies the user when done. Notifications about final papers never include their questions.
- `export.py`: locked final papers and answer keys as PDF and Word.

### HR (`hr/`)
Employees, departments with heads and line managers, attendance (punch in/out, late grace in settings), holidays,
and leave with balances. Business logic is in `hr/services.py` (`review_leave`, `cancel_leave`, `check_in`/`check_out`,
working days that skip `HR_WEEKEND_DAYS` = Fri/Sat). Monthly reports with CSV export are in `hr/reports.py`.

## Tests
Tests are in each app's `tests.py`. Claude calls are mocked with `mock.patch.object(ai, "_ask_claude", ...)`, and
background threads are suppressed by patching `ai.threading.Thread`. Uploads go to a temporary `MEDIA_ROOT`.

## Conventions seen in the codebase
- Docstrings and comments explain *why*, in plain language. User-facing messages are written for school staff, not developers.
- Commit messages are short, imperative and describe the user-visible change.
