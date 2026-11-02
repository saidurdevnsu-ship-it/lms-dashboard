---
name: django-development
description: Use when adding features or fixing bugs in STS Pulse code. Where things go and the house patterns.
---

# Changing STS Pulse

Read first: `docs/architecture.md`. Also `docs/database-design.md` for model/migration work,
`docs/business-requirements.md` to check the feature is in scope (RAG and the hybrid workflow are deferred), and
the current plan in `.hermes/plans/` if the task is one of its phases.

## Where code goes

- Rules and state changes: `questionbank/workflow.py`, `questionbank/ai.py`, `hr/services.py` — not in admin
  classes or views. Admin calls the service and shows its error with `message_user`.
- UI: Django admin only — `ModelAdmin`, `get_urls` custom views, `templates/admin/<app>/...`,
  `static/css/admin-*.css`. No separate frontend.
- A view in app A editing app B's model uses a `ModelForm` owned by app B (form limits follow the model; MySQL
  strict mode turns overlong input into a 500 otherwise).

## Patterns to reuse

- Scope: `scope.in_scope(qs, user, prefix)`, `scope.covers(user, subject, grade)`.
- Reviews: `learning.approvals.is_own` / `others_only`, `SELF_APPROVAL_MESSAGE`.
- Side effects: `audit.services.record(actor, Action.X, target, details=...)` (add a new `Action` choice + migration
  if needed), `notify(users, title, body=, url=, kind=, dedupe_key=, exclude=)`, `users_with_perm(perm)`,
  `send_email(...)`, `send_event(...)`.
- Claude: only `ai._ask_claude(system, messages, schema, ...)`; anything slow runs in
  `threading.Thread(target=_in_thread, args=(func, ...), daemon=True)` and notifies when done.
- Roles: change defaults in `learning/roles.py` `DEFAULT_ROLES`; existing installs keep their edits unless reset.
  Never give `manage_final_papers` to a role other than Exam Controller.
- Save only when `form.has_changed()`; wrap multi-form saves in `transaction.atomic()`. `@never_cache` on staff
  pages outside `admin_view` that show personal data.

## Done means

1. Test first (`skills/testing/SKILL.md`), then implement.
2. Full suite on MySQL and SQLite, `manage.py check`, `makemigrations --check --dry-run`.
3. Report: what changed (file, why), test counts, open questions. No commit, push, `migrate` on the real DB or
   package install without the user's yes.
