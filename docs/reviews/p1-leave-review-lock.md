# Review: p1-leave-review-lock

Verdict: APPROVED

Scope reviewed: uncommitted `hr/admin.py`, `hr/services.py`, `hr/tests.py`, untracked `hr/roles.py` (branch
`agent/p1-leave-review-lock`; `git diff main...HEAD` empty), against `docs/tasks/p1-leave-review-lock.md`.
Also read the code they depend on: `hr/models.py` (LeaveRequest/Employee/Department), `notifications/make.py`
(`send_event`), `notifications/services.py` (`notify`), `learning/approvals.py` (`is_own`), `config/settings.py`
(MySQL isolation level), `hr/templatetags/hr_tags.py` (import of `is_hr_manager`).

Verified by the reviewer (SQLite only, MySQL is QA's job): `manage.py test hr` -> 37 tests OK (2 skipped: the
MySQL-only concurrency tests); `manage.py check` clean; `makemigrations --check --dry-run` -> No changes detected.

## Findings

| # | Severity | File:line | Issue | Concrete fix |
|---|---|---|---|---|
| 1 | Low | hr/admin.py:307-313 (`save_model`, change form) | The HR change form still saves the whole row without the lock. Django's `changeform_view` loads the request, then `obj.save()` writes every column, `status`/`reviewed_by`/`reviewed_at` included. If an approval commits between that load and the save (same HTTP request, milliseconds), the approval is overwritten back to *pending* while its attendance rows and audit entry stay, and the request can then be reviewed a second time. This is a narrow race that already existed, and it isn't a permission bypass (the change form is HR-only and `status` is read-only). It belongs to the same class of bug this task fixes, though. (Theoretical extra: if HR also changes `employee` on the form, InnoDB's FK check on the employee row can deadlock against `lock_leave`. MySQL would detect it and roll one side back.) | Follow-up task (or this one if the Architect wants): in `save_model` for `change=True`, lock with `services.lock_leave(obj)`-style ordering (employee, then request) inside `transaction.atomic`, refuse if no longer pending, and save with `update_fields` that leave out `status`, `reviewed_by`, `reviewed_at`. |
| 2 | Low | hr/admin.py:325, hr/admin.py:354 | The admin actions still skip some selected rows without a message: approve/reject skip non-pending rows, and cancel skips rejected/cancelled rows. Only the total ("Approved 0 pending leave request(s).", WARNING) shows it. Permission refusals are now reported per row, which is what the criterion asks for, so this is about clarity only. | Optionally report skipped rows too, e.g. one line "N selected request(s) were not pending and were left alone." |
| 3 | Low | hr/services.py:192, hr/services.py:218 | `notify` now runs in `on_commit` after the change is saved. If it fails (DB error creating the Notification row), the exception escapes from the outer `atomic` exit. In the admin loop that becomes a 500 after the leave was already approved, and the rest of the selected rows are never processed. Before, the same failure rolled the review back. | `transaction.on_commit(after_commit, robust=True)` (Django 4.2+), so a notification failure is logged and the saved review stands. Same for the cancel lambda. |
| 4 | Info | hr/services.py:157-164 | The serialisation of two different requests depends on MySQL running READ COMMITTED (`config/settings.py:134`). That holds today. Under REPEATABLE READ, any plain read before the lock (`review_refusal` -> `has_perm`/lazy `employee.department` loads) would fix the snapshot, and the balance check under the lock would read stale `leave_used`. | Optional: one line in the `lock_leave` docstring saying the balance re-check relies on READ COMMITTED, so nobody "fixes" the isolation level later without knowing. |
| 5 | Info | hr/tests.py:470-476, 483 | Concurrency test diagnostics: (a) `second()` catches only `LeaveError`. A lock-wait timeout or deadlock (`OperationalError`) kills the thread with a traceback, and the test then fails with an unhelpful `None != "This request was already approved."`. (b) `b.join(1.5)` + `is_alive()` proves "still running after 1.5 s", which can't give a false failure when the lock works (InnoDB waits up to 50 s), but a very slow thread start could give a false pass. | (a) Catch `Exception` in `second()` and store `repr(exc)` so the assertion shows the real error. (b) Acceptable as is. For a stricter proof, also assert `"error" not in outcome` right before `release.set()`. |
| 6 | Info | hr/tests.py:325 | `test_two_requests_over_the_balance_cannot_both_be_approved_from_stale_copies` would also pass without any lock, because `balance_problems` always queries fresh data. It proves the sequential stale-copy case, not concurrency. The real concurrency proof is the MySQL-only `test_two_requests_over_the_balance_cannot_both_be_approved_at_once`. That's fine, as long as QA actually runs the MySQL suite. | None. QA must run MySQL so the two skipped tests execute. |

No Critical, High or Medium findings.

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| `review_leave` in one transaction, locks employee then request (`select_for_update`), re-reads, refuses non-pending with plain `LeaveError` ("This request was already approved/rejected/cancelled.") | Met | `@transaction.atomic` + `lock_leave` (services.py:136-148, 160-162); `already_done` wording; `refresh_from_db(from_queryset=LeaveRequest.objects.select_for_update())` runs inside the atomic (no TransactionManagementError), and on SQLite `select_for_update` is a no-op. Employee change between read and lock is detected. |
| Balance check under that lock, so two requests of the same employee can't both pass | Met (MySQL proof pending QA) | `balance_problems` runs after `lock_leave`. Both approvals lock the same Employee row first, so the second waits for the first to commit, and under READ COMMITTED it then sees the first approval. MySQL test `test_two_requests_over_the_balance_cannot_both_be_approved_at_once`. |
| `can_review_leave(user, leave)` in services: HR or head of employee's department, never own; enforced in `review_leave`; admin uses it to filter; same messages | Met | services.py:99-110, 157-159. `SELF_APPROVAL_MESSAGE` checked first (so superusers can't review their own). Admin `_review` uses `review_refusal` and reports refusals. |
| `cancel_leave` same lock + re-check; `can_cancel_leave`: employee only own PENDING, HR pending or approved of anyone, heads/others refused; admin offers only what's allowed and reports refusals | Met | services.py:113-129, 195-206. Permission is checked before the lock and again after it on the fresh row. The admin reports "Not cancelled: … <reason>". Tests cover head (pending and approved), employee (own pending ok, own approved refused), stranger, HR. |
| Notifications, Make `leave.approved`, audit only once saved (`on_commit` for outside calls), never for refused/rolled-back review | Met | `record` stays inside the transaction. `send_event` + `notify` sit in `on_commit` (services.py:186-192, 218-220). `send_event` already defers its own HTTP call with `on_commit` + thread. Tests: rolled-back review, refused approval, and stale review all produce no callbacks. |
| Tests: stale approval refused and changes nothing; not-allowed review/cancel refused in service; two over-balance requests not both approved; existing tests pass; MySQL-only two-connection `TransactionTestCase` | Met | `LeaveReviewLockTests`, `LeaveCancelRuleTests`, `ConcurrentLeaveReviewTests` (skipped unless `connection.vendor == "mysql"`). Threads close their connections, and TransactionTestCase flushes afterwards. 37/37 SQLite. |
| Full suite on MySQL and SQLite; `check` clean; `makemigrations --check` no changes | Partly verified | SQLite `hr` suite, `check` and `makemigrations --check` pass (reviewer). Full suite on both backends: QA. |
| Out of scope untouched | Met | Diff touches only `hr/admin.py`, `hr/services.py`, `hr/tests.py`, plus new `hr/roles.py`. Leave rules and wording are unchanged apart from the new refusal messages. No migration. |

Lock order: every locking path (`review_leave`, `cancel_leave`) goes through `lock_leave`, which takes the employee
first, then the request. `Attendance.update_or_create` inside the review only adds FK share locks on the
employee row the transaction already holds exclusively. No review/cancel deadlock cycle found.

`hr/roles.py` has no imports, so there is no circular-import risk. `hr.admin` re-exports `is_hr_manager`
(admin.py:21), so `hr/templatetags/hr_tags.py:7` (`from hr.admin import is_department_head, is_hr_manager`) still
works. Codebase Memory shows all 20 callers resolving to `hr.roles.is_hr_manager`. The HR dashboard tests pass.

## Questions

1. Finding 1 (change-form race): fix it in this task, or open a small follow-up? It isn't listed in the contract,
   so I didn't treat it as an unmet criterion.
2. Untracked files that aren't part of this task are in the working tree: `.mcp.json`,
   `docs/spec-status-2026-11-03.md`, `for testing the q generation feature.txt`. Make sure they aren't staged with
   this task's commit.

## Done well

- One `lock_leave` helper gives a single, documented lock order, and it detects an employee swap between read
  and lock.
- Permission is checked before the lock (cheap, clear message) and again on the fresh row for cancel. For
  review, the employee-unchanged check makes the second check unnecessary.
- `already_done` gives plain, accurate wording for staff, and admin messages name the request and the reason.
- Outside calls are captured into locals before `on_commit`, so the callbacks don't depend on later changes to
  the instance.
- Tests check what matters, not just the exception: attendance rows, audit counts, notification counts, captured
  on-commit callbacks, the caller's refreshed copy, and admin flash messages.
- The concurrency test is sensibly built: an explicit hold/release, bounded waits, a connection closed per
  thread, and a clear skip reason.

## History

Round 1 (2026-11-04): APPROVED
