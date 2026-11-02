# Database design

Derived from the `models.py` files. `learning` has no models. Users and roles are Django's `auth.User` and
`auth.Group` (shown as "roles").

## questionbank

| Model | Key fields / relations | Constraints, notes |
|---|---|---|
| `Subject` | `name` (unique), `code` | |
| `Grade` | `name` (unique), `order` | ordered by `order, name` |
| `SubjectAssignment` | `user` FK, `subject` FK, `grade` FK null (= all grades) | unique `(user, subject, grade)`; conditional unique `one_all_grades_assignment` (grade null) — **not created on MySQL**, also enforced in admin form validation |
| `KnowledgeSource` | `subject`, `grade` FK; `title`, `material_type` (textbook/syllabus/lesson_plan/sample_questions/notes), `chapter`, `topic`, `file` (pdf/docx/txt/md, max 20 MB), `is_active`, `content_text` (extracted), `uploaded_by` | only active sources are used for generation |
| `Question` | `subject`, `grade` FK PROTECT; `question_type` (mcq/short/long/true_false/fill_blank), `difficulty`, `text`, `options` JSON list, `answer`, `explanation`, `source_reference`, `status` (draft/approved/rejected), `ai_generated`, `duplicate_of` self-FK, `created_by` | index `(subject, grade, status)`; permission `approve_question` |
| `AcademicEvent` | `title`, `event_type` (term/exam_series/internal_exam/question_deadline/break/other), `exam_series`, `qualification` (Cambridge levels), `start_date`, `end_date` null, optional `subject`/`grade` | `clean()` rejects end before start |
| `QuestionBlueprint` | `title`, `subject`/`grade` PROTECT, `exam` FK AcademicEvent (exam types only), `assigned_to` PROTECT, `due_date`, `duration_minutes`, `language` (en/bn), `sample_file`, `materials` M2M KnowledgeSource, `instructions`, `guidance`, `status` (draft/in_review/returned/approved/finalized), review fields | permissions `review_blueprint`, `manage_final_papers`; author edits only in draft/returned |
| `BlueprintItem` | `blueprint` FK CASCADE (`items`), `order`, `section`, `question_type`, `difficulty`, `topic`, `count`, `marks_each`, `sample_question` | `count`, `marks_each` ≥ 1 |
| `FinalPaper` | `blueprint` OneToOne PROTECT (`final_paper`), `status` (generated/locked), `model_used`, generated/locked by+at, `controller_notes`, `generation_started_at`, `generation_error`, `check_summary` | `default_permissions = ()`: access only via `manage_final_papers`; `GENERATION_TIMEOUT` 15 min |
| `FinalQuestion` | `paper` FK CASCADE (`questions`), `order`, `section`, `question_type`, `text`, `options` JSON, `answer`, `explanation`, `marks`, `source_reference`, `check_notes` | `default_permissions = ()` |

Planned (not yet in models): `AIUsage` token counts per Claude call — see `.hermes/plans/`.

## hr

| Model | Key fields / relations | Constraints, notes |
|---|---|---|
| `Department` | `name` unique, `head` FK user null | head sees the department's records and approves its leave |
| `Designation` | `name` unique | |
| `Employee` | `user` OneToOne PROTECT (`employee`), `employee_id` unique, `department`/`designation` PROTECT, `reports_to` self-FK (line manager), `joining_date`, contact and bank fields, `is_active` | permission `manage_hr` |
| `Holiday` | `date` unique, `name` | excluded from working days |
| `Attendance` | `employee` FK, `date`, `status` (present/late/half_day/absent/on_leave), `check_in`, `check_out`, `note` | unique `(employee, date)` |
| `LeaveType` | `name` unique, `days_per_year` (0 = unlimited), `is_paid` | defaults created by `setup_hr` |
| `LeaveRequest` | `employee` FK, `leave_type` PROTECT, `start_date`, `end_date`, `days` (working days, computed), `reason`, `status` (pending/approved/rejected/cancelled), review fields | check constraint `end_date >= start_date` |

## notifications

`Notification`: `recipient` FK, `kind` (approval/reminder/success/warning/info), `title`, `body`, `url`, `is_read`,
`dedupe_key` (nullable), `created_at`. Index `(recipient, is_read)`; unique `(recipient, dedupe_key)`.
`dedupe_key` is NULL (not `""`) when unused: NULLs never clash, which makes the "once per key" rule work without a
conditional constraint on MySQL.

## audit

`AuditEvent`: `created_at`, `actor` FK null + `actor_name` (kept after account deletion), `action` (choices in
`AuditEvent.Action`: sign-ins, admin add/change/delete, question generation/approval, blueprint and final-paper
steps, leave decisions, exports), `target_type`, `target_id`, `target`, `details`, `ip_address`. Never holds
final-paper questions or passwords. Read-only in admin.

## Migrations

audit 0002, hr 0003, learning 0002, notifications 0002, questionbank 0009 (latest applied in the repo). Check drift
with `makemigrations --check --dry-run`. The post_migrate hook creates missing permissions and roles on every
`migrate`.

## MySQL vs SQLite

`DB_NAME` set → MySQL 8.4+ (local Windows service `MySQL84`, database `lms`, test database `test_lms`); unset →
`db.sqlite3`. MySQL options: `utf8mb4` (Bangla and emoji), isolation `read committed`,
`sql_mode='STRICT_TRANS_TABLES'`, test collation `utf8mb4_0900_ai_ci`. The local `.env` currently uses MySQL.
MySQL credentials live outside the repo in `C:\Users\User\AppData\Local\MySQL-dev\` — never print or copy them.

MySQL pitfalls (run the tests on both backends to catch them):
- **No conditional unique constraints.** `models.W036` is silenced for MySQL; `questionbank.tests` lists the allowed
  conditional constraints (`test_only_known_conditional_constraints_exist`), so a new one fails until it is also
  enforced by validation.
- **Case-insensitive collation**: `employee` == `Employee` in lookups and unique checks.
- **`bulk_create` returns objects without ids.** Save one by one (or re-query) when callers need pks.
- **Time zone tables must be loaded**, or date-based admin pages (the audit log's date filter) fail with "invalid
  datetime value". Linux: `mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root mysql`. Windows: load
  `timezone_posix.sql` from https://dev.mysql.com/downloads/timezones.html into the `mysql` schema. Check:
  `SELECT CONVERT_TZ(NOW(), 'UTC', 'Asia/Dhaka')` is not NULL. Prefer aware datetime ranges over DB date functions.
- **`loaddata` fires post_save with `raw=True`**; signal receivers that write must skip raw saves (see
  `audit/signals.py`).
- **Strict mode**: overlong input raises instead of truncating; keep form limits derived from model fields.
- `mysqlclient` is in `requirements.txt`, so a Linux server needs the MySQL client headers and a compiler to install
  it (e.g. `default-libmysqlclient-dev build-essential pkg-config` on Debian/Ubuntu), even if it stays on SQLite.

## Backups and migrating (only with the user's explicit yes)

`backups/` is git-ignored. Back up before every `migrate` on a real database, even when `showmigrations` shows
nothing pending (post_migrate still runs); compare a few row counts before and after.

- SQLite: copy `db.sqlite3` to `backups/db-before-<what>-<date>.sqlite3`, `cmp` it, run `pragma integrity_check`.
- MySQL: `mysqldump --single-transaction lms > backups/lms-before-<migration>-<date>.sql`, using a temporary
  `--defaults-extra-file` built from the credentials in `MySQL-dev\` and deleted afterwards (recipe in the current
  plan under `.hermes/plans/`). Then `venv/Scripts/python.exe manage.py migrate <app>`.
