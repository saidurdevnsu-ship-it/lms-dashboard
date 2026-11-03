# Architecture

"STS Pulse": a Django 6.1 school LMS + HR system for STS Group. There is no separate frontend: everything runs inside
**Django admin**, customised with overridden templates in `templates/admin/`, custom admin views (via `get_urls`) and
the CSS in `static/css/admin-*.css` (plus `login.css`, `print-doc.css`, `punch.css`). Time zone `Asia/Dhaka`;
content can be English or Bangla.

## Apps

| App | Responsibility | Key modules |
|---|---|---|
| `learning` | Sign-in page, workspaces, roles, user admin, welcome email, My profile. Holds no models (the old course models were removed 2026-10-28). | `workspaces.py`, `roles.py`, `approvals.py`, `views.py`, `admin.py`, `welcome.py` |
| `questionbank` | The Academic workspace: subjects/grades, subject assignments, knowledge sources, AI question generation, blueprints, confidential final papers, exports, reminders. | `ai.py`, `workflow.py`, `scope.py`, `checks.py`, `documents.py`, `export.py`, `reminders.py`, `papers.py` |
| `hr` | The HR workspace: employees, departments, designations, attendance, holidays, leave, reports. | `services.py`, `reports.py`, `admin.py` |
| `notifications` | In-app notifications, email, Make.com events. | `services.py`, `mail.py`, `make.py`, `context_processors.py` |
| `audit` | Read-only audit log. | `services.py`, `signals.py` |

`config/urls.py`: `/` → `learning.views.home`; `/admin/login/` redirects to the shared `/accounts/login/`
(`RememberMeLoginView`); `/admin/` is routed to `learning.views.dashboard` first (sends the user to the workspace
picker or the HR home), then to `admin.site.urls`; `/profile/`, `/workspace/`, `/workspace/<name>/`,
`/notifications/`; `/media/...` is served to signed-in staff only (`staff_member_required(serve)`), in development
and on the server.

## Two workspaces, one sign-in (`learning/workspaces.py`)

- After sign-in users pick **Academic** (`questionbank`) or **HR** (`hr` + `auth`) and see only that workspace.
  `notifications` and `audit` belong to neither and show in both. A workspace is available when the user has module
  perms on any of its apps.
- The choice is stored in the session (`SESSION_KEY = "workspace"`); `current()` re-checks availability because
  roles can change during a long session.
- `LearningConfig.ready()` monkey-patches `admin.site.get_app_list` to hide the other workspace's apps, renames
  Group to "role"/"roles", and connects the post_migrate role hook.
- `WorkspaceMiddleware` switches workspace when someone opens a URL belonging to the other one.
- Notifications that link into a hidden workspace are filtered with `without_hidden_links`.

## Roles = Django Groups (`learning/roles.py`)

`DEFAULT_ROLES` defines the predefined roles and their permissions (`app.codename`, `app.*` wildcards, a `-` prefix
excludes): Teacher / Subject Expert, Head of Department, Exam Controller, Read-Only, HR Manager, Employee.
Roles are created only when missing (post_migrate `create_missing_roles`, and `setup_academics` / `setup_hr`), so
admin edits persist; they are reset only on request (`--reset` or the "Reset to default permissions" admin action).
`keep_final_papers_confidential` strips `questionbank.manage_final_papers` from every role except Exam Controller
and runs after every `ensure_roles`. Role permissions per role: see `business-requirements.md`.

User/role admin (`learning/admin.py`): only super admins create roles or change their permissions, and manage
subject assignments; non-superusers can only grant roles within their own permissions (no privilege escalation).
HR Manager has view-only access to accounts.

## Cross-cutting helpers (always use these)

- **Subject scope** (`questionbank/scope.py`): non-superusers see only the subjects and grades in their
  `SubjectAssignment`s (null grade = all grades), and nothing without one. Superusers and holders of
  `manage_final_papers` see everything. Admin querysets go through `in_scope(qs, user, prefix)`; single checks use
  `covers(user, subject, grade)`.
- **No self-approval** (`learning/approvals.py`): nobody, superusers included, approves their own question,
  blueprint or leave request. Use `is_own` / `others_only` (and `SELF_APPROVAL_MESSAGE`) in any review flow.
- **Audit** (`audit/`): `audit.services.record(actor, Action.X, target, ...)` for significant actions. Admin
  add/change/delete is logged automatically via a `LogEntry` post_save signal (skips `raw=True` saves from
  `loaddata`), sign-ins via auth signals. The log is read-only and never stores final-paper questions or passwords.
- **Notifications** (`notifications.services.notify`): in-app, optionally deduplicated with `dedupe_key`; recipients
  via `users_with_perm(perm)`. `questionbank.reminders.ensure_reminders` also runs lazily from the notifications
  context processor on page loads (and daily via `send_reminders`).
- **Email**: `notifications.mail.send_email` (absolute links via `site_url`; goes through Make when
  `MAKE_WEBHOOK_URL` is set, otherwise Django's mailer; raises on failure so callers decide).
- **Make.com**: `notifications.make.send_event` sends after `transaction.on_commit` from a daemon thread; failures
  are logged, never raised.
- **Business-rule errors**: services raise exceptions with user-facing messages — `workflow.WorkflowError`,
  `ai.GenerationError`, `hr.services.LeaveError` — which the admin shows as messages.

## Question-paper flow (`questionbank/`)

1. **Materials**: `KnowledgeSource` (PDF, DOCX, TXT, MD up to 20 MB) per subject/grade, with type, chapter and topic.
   `documents.py` extracts text on upload (pymupdf, python-docx) into `content_text`.
2. **Practice/bank questions**: `ai.generate_questions` asks the AI provider (structured JSON output) using chosen sources;
   results are draft `Question`s. `checks.py` flags near-duplicates and questions copied from samples. HOD
   (`approve_question`) approves/rejects, never their own.
3. **Blueprint** (`QuestionBlueprint` + `BlueprintItem`s), state machine in `workflow.py`:
   Teacher/SME drafts → `submit` (IN_REVIEW) → HOD `approve` (APPROVED, with exam controller) or
   `return_to_author` (RETURNED, editable again) → exam controller generates → FINALIZED.
4. **Confidential final paper**: `ai.start_final_paper` runs `write_final_paper` in a `threading.Thread` via
   `_in_thread` (closes its DB connection) because generation takes minutes, then notifies the user.
   `generate_final_paper` forces question count, sections, types and marks from the blueprint;
   `check_final_questions` runs an AI review of syllabus fit and answers. `FinalPaper.GENERATION_TIMEOUT` (15 min)
   unblocks a stuck run. The exam controller edits questions inline, regenerates, then `lock_final`.
5. **Export** (`export.py`): locked papers only, as PDF and Word, with optional answer key (audited).

The AI is called only through `ai._ask_claude` → `llm.ask`. Notifications about final papers never include their
questions.

### AI provider layer (`questionbank/llm.py`)

- `ai.py` builds every request once in the Anthropic message format (text / document blocks, `cache_control`) and
  sends it through `ai._ask_claude` (the permanent seam tests patch) → `llm.ask`. `llm.ask` picks the adapter from
  `AI_PROVIDER` (`anthropic`, default, or `gemini`): `_ask_anthropic` sends the request unchanged (streamed,
  structured output, server-side fallback); `_ask_gemini` translates it to google-genai parts. Both return a
  `Reply` with token counts, so usage recording (`AIUsage`, never content), error messages and JSON parsing are
  shared.
- Models: `AI_MODEL` (bank questions) and `AI_FINAL_MODEL` (final papers, their review pass and single-question
  rewrites). Left empty they fall back to `QUESTION_GENERATION_MODEL` on Anthropic and the Gemini defaults on
  Gemini; a model id from the other provider's family is refused before any request.
- Paid-tier guard: on Gemini, final-paper purposes are refused (and the admin buttons disabled) unless
  `GEMINI_PAID_TIER` is `true`/`1`, because Google may use free-tier prompts for training and human review.
- Gemini requests always send `store=False` and put PDFs inline as bytes (never the Files API), so no copy of a
  book or paper stays in the Google project. The key comes only from `GEMINI_API_KEY` (passed explicitly, so
  `GOOGLE_API_KEY` can't win) with `vertexai=False`; one client per request, closed afterwards.
- Logs name the purpose, request id and sizes, never prompt or reply text (final papers are confidential).
- `check_ai` (alias `check_claude`) sends one tiny paid request per model to check the key and models. In progress (see `.hermes/plans/`): `papers.py` blueprint slots and
`workflow.paper_problems` blocking a lock when the paper no longer matches its blueprint.

## HR flow (`hr/`)

- Employees (one per user account) with department, designation and line manager (`reports_to`); departments have
  a `head`.
- Visibility: HR managers (`hr.manage_hr`) see everyone; department heads see their departments' records (not bank
  details) and their own; other staff only their own. Attendance is HR's alone to edit.
- Attendance: punch in/out (`check_in` / `check_out`); late after `HR_OFFICE_START` (09:00) +
  `HR_LATE_GRACE_MINUTES` (15). Approved leave writes ON_LEAVE attendance rows.
- Leave: requests notify HR managers and the department head (`announce_request`); `review_leave` re-checks the
  balance on approval, records audit, sends the Make `leave.approved` event and notifies; `cancel_leave` removes the
  ON_LEAVE rows. Working days skip `HR_WEEKEND_DAYS` (Fri/Sat) and `Holiday`s.
- Reports: `hr/reports.py` monthly attendance + leave report with worked hours and CSV export (audited).

## Configuration (`config/settings.py`)

Reads a git-ignored `.env` with its own loader (`os.environ.setdefault`: real environment variables win, even empty
ones). `stslms.env` is the committed template (key names only). Switches:

- `DJANGO_SECRET_KEY` required; startup fails without it (no fallback by design).
- `DB_NAME` set → MySQL; else `db.sqlite3`. See `database-design.md`.
- `EMAIL_HOST` set → SMTP; else emails print to the console. Uses Django 6's `MAILERS` setting, not `EMAIL_BACKEND`.
- `DJANGO_DEBUG=true` for local development. `SITE_URL` sets absolute email links, `ALLOWED_HOSTS` and CSRF origins
  (and secure cookies when https and not DEBUG). `DJANGO_ALLOWED_HOSTS` adds hosts.
- `AI_PROVIDER` (`anthropic` default, or `gemini`), `AI_MODEL`, `AI_FINAL_MODEL`; `ANTHROPIC_API_KEY`,
  `ANTHROPIC_WORKSPACE_ID`, `QUESTION_GENERATION_MODEL` (default `claude-opus-5-5`) for Claude; `GEMINI_API_KEY`
  and `GEMINI_PAID_TIER` (`true`/`1` only) for Gemini. See the provider layer above.
- `MAKE_WEBHOOK_URL` / `MAKE_MAILHOOK_ADDRESS` for Make.com events; with the webhook set, all emails go through Make.
- HR constants in settings: `HR_COMPANY_NAME`, `HR_WEEKEND_DAYS = [4, 5]`, `HR_OFFICE_START`, `HR_LATE_GRACE_MINUTES`.
