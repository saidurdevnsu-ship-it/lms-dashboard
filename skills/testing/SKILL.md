---
name: testing
description: Use when writing, running or fixing tests in STS Pulse. Runs them side-effect free on MySQL and SQLite.
---

# Testing STS Pulse

Read first: `docs/development-workflow.md` (commands), `docs/database-design.md` (MySQL pitfalls).

## Run

```bash
T="env MAKE_WEBHOOK_URL= MAKE_MAILHOOK_ADDRESS= ANTHROPIC_API_KEY= EMAIL_HOST= AI_PROVIDER= AI_MODEL= AI_FINAL_MODEL= GEMINI_API_KEY= GEMINI_PAID_TIER="
$T venv/Scripts/python.exe manage.py test <app>[.tests.Class.test] --noinput   # MySQL
$T DB_NAME= venv/Scripts/python.exe manage.py test --noinput                   # SQLite
```

Never run `manage.py test` without the `$T` prefix: `.env` holds a live webhook and API keys, and an
`AI_PROVIDER=gemini` there would route tests to Gemini. Finish with the full
suite on **both** backends (~119 tests, ~1.5 min each), `manage.py check` and `makemigrations --check --dry-run`,
and confirm `git status --short` shows only your intended changes.

## Write

- Tests live in each app's single `tests.py`. Follow TDD: see the new test fail for the stated reason first.
- Never call the AI: `mock.patch.object(ai, "_ask_claude", return_value=...)` (patch `ai.anthropic.Anthropic` /
  `llm.genai.Client` only when testing the adapters in `questionbank/test_llm.py`). Classes touching AI settings
  pin them (`questionbank.tests.AI_DEFAULTS`, inherited by `BlueprintTestCase`); tests that build a client also
  patch the key with `mock.patch.dict("os.environ", ...)`. Suppress background threads with `mock.patch.object(ai.threading, "Thread")`;
  to exercise the background body, call `write_final_paper(...)` directly.
- Any class whose code may send email or Make events gets
  `@override_settings(MAKE_WEBHOOK_URL="", MAKE_MAILHOOK_ADDRESS="")`.
- Uploads go to a temporary `MEDIA_ROOT` (override it; never write to `media/`).
- Cover permissions per role: scope (`in_scope`/`covers`), no self-approval (superusers too), and that final papers
  stay hidden from Teacher/HOD/Read-Only and out of notification text.
- Admin inline POSTs: fields with callable defaults (`JSONField(default=list)`) render a hidden
  `initial-<prefix>-<n>-<field>` input; include it in the POST or every form looks changed. The prefix is the FK's
  related_name (e.g. `questions-0-text`).
- MySQL-safe assertions: don't rely on ids returned by `bulk_create`, on case-sensitive uniqueness, or on DB date
  functions. A new conditional unique constraint must be added to the allowed list in
  `questionbank.tests.test_only_known_conditional_constraints_exist` *and* enforced by validation.
- Don't touch the real database; tests use `test_lms` (MySQL) or in-memory SQLite.
