# Development workflow

Windows 11, Python 3.15 venv at `venv/`, Django 6.1.2. The agent terminal is git-bash: POSIX syntax, but native
tools (python, git) need `C:/...` paths, not MSYS paths like `/tmp`.

## Setup

```bash
cp stslms.env .env            # then fill in values; at least DJANGO_SECRET_KEY and DJANGO_DEBUG=true
venv/Scripts/python.exe -m pip install -r requirements.txt    # only with the user's yes
venv/Scripts/python.exe manage.py migrate                     # creates missing roles too; see "Approval rules"
venv/Scripts/python.exe manage.py runserver
```

`.env` is git-ignored and holds live values (API key, Make webhook, DB credentials). Never print `.env` values;
report key names only.

## Commands

```bash
venv/Scripts/python.exe manage.py check
venv/Scripts/python.exe manage.py makemigrations --check --dry-run   # drift check, writes nothing
venv/Scripts/python.exe manage.py makemigrations <app>
venv/Scripts/python.exe manage.py runserver
```

Management commands:
- `setup_academics [--reset]` / `setup_hr [--reset]`: create the predefined roles (and default leave types for HR);
  `--reset` restores default permissions of existing roles.
- `send_reminders`: create today's deadline reminders and email the new ones (daily schedule).
- `check_claude`: one tiny **paid** Anthropic request to check key and model — ask first.
- `send_make_test [events...]`: sends sample payloads to the **real** Make webhook/mailhook — ask first.

No linter, formatter or CI is configured.

## Testing without side effects

Blank every integration for the test process (the settings loader never overrides an already-set variable, even an
empty one):

```bash
T="env MAKE_WEBHOOK_URL= MAKE_MAILHOOK_ADDRESS= ANTHROPIC_API_KEY= EMAIL_HOST="
$T venv/Scripts/python.exe manage.py test --noinput                    # MySQL (test_lms), the default
$T DB_NAME= venv/Scripts/python.exe manage.py test --noinput           # SQLite
$T venv/Scripts/python.exe manage.py test questionbank --noinput       # one app
$T venv/Scripts/python.exe manage.py test questionbank.tests.FinalPaperTests.test_x --noinput   # one test
```

Full suite ≈119 tests, ~1.5 min per backend. Run both backends before calling work done: MySQL-only problems
(conditional constraints, collation, `bulk_create` ids, time zone tables) don't show on SQLite. See
`skills/testing/SKILL.md` for test conventions.

## Commit style

One short imperative line describing the user-visible change, optional bullet body. Examples from `git log`:
"Add department heads and line managers to HR", "Apply one no-self-approval rule to questions, blueprints and
leave", "Export locked final papers and answer keys as PDF and Word".

## Approval rules (the user's)

- No `git commit` or `git push` without an explicit yes; pushing is a separate yes. Prepare the message and diff
  summary, then ask.
- No `migrate` on the real database without a yes, and back up first (`database-design.md`, "Backups").
- No package installs/upgrades/uninstalls without a yes.
- No paid API calls (`check_claude`, real generation) or real webhook sends (`send_make_test`) without a yes.
- When asked to review or assess, change nothing; report and ask which steps to take.

## Windows pitfalls

- Native `git.exe` can't read MSYS paths like `/tmp`; write message files under
  `C:/Users/User/AppData/Local/hermes/cache/scratch/`.
- Git "LF will be replaced by CRLF" warnings: filter them when reading diffs, never hide stderr on writing commands.
- Pyright "Import django could not be resolved" is noise (language server isn't using the venv); trust the tests.
- MySQL runs as the Windows service `MySQL84`; its credentials are in `C:\Users\User\AppData\Local\MySQL-dev\` —
  never print or copy them.
