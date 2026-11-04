# Task: p1-leave-review-lock

## Goal
Make approving, rejecting and cancelling leave safe when two people act at the same time, and move the
"who may do this" rules out of the admin into `hr/services.py`, so a request can never be reviewed twice or
take its days out of the balance twice. (Review findings CODE-10, DB-05.)

## Acceptance criteria
- [ ] `review_leave` runs in one transaction: it locks the employee row and the request row
      (`select_for_update`), re-reads the request, and refuses with a plain `LeaveError` if it is no longer
      pending ("This request was already approved/rejected/cancelled.").
- [ ] The balance check runs under that lock, so two different requests of the same employee approved at the
      same time can't both pass when only one fits.
- [ ] Who may review moves into services (`can_review_leave(user, leave)`): HR manager, or the head of the
      employee's department; never the employee themselves. `review_leave` enforces it; the admin action only
      uses it to filter what it offers. Same messages as today.
- [ ] `cancel_leave` gets the same lock + re-check, and a stricter "who may cancel" rule in services
      (`can_cancel_leave`, decided by the user, closes SR-15): the employee may withdraw only their OWN pending
      request; an HR manager may cancel pending or approved requests of anyone. Department heads (unless HR) and
      anyone else are refused with a plain `LeaveError`. The admin cancel action offers only what the user may
      cancel and reports refusals.
- [ ] Notifications, the Make `leave.approved` event and audit records happen only once the change is saved
      (`transaction.on_commit` for the outside calls), never for a refused or rolled-back review.
- [ ] Tests: approving a stale copy of an already-reviewed request is refused and changes nothing (no second
      attendance rows, no second notification/event); review/cancel by someone not allowed is refused in the
      service, not only hidden in the admin; two requests that together exceed the balance can't both be approved;
      existing HR tests still pass. A MySQL-only concurrency test (two connections, `TransactionTestCase`) proves
      the second reviewer waits and is refused (skipped on SQLite, which serialises writes anyway).
- [ ] Full suite passes on MySQL and SQLite; `manage.py check` clean; `makemigrations --check` reports no
      changes (no migration expected).

## Out of scope
- Employee/Subject/Grade delete protection (P1 item 1: dropped by the user; admins may delete).
- Final-paper generation guard (next task) and AI example fixes (task after).
- Any change to leave rules themselves (balances, weekends, holidays), wording beyond the new refusal messages,
  or the live database.

## Code map
<Filled in by the planner from the code index (Codebase Memory) or by reading the source. For orientation:
agents must read the current source before editing.>
Index: ready, indexed_at 2026-11-04T05:46:37Z, HEAD d84e1bd (hr/ last changed 310a37b, 2026-11-02; no uncommitted edits)

| Symbol | File:lines | Callers | Calls into | Covering tests |
|---|---|---|---|---|
| `hr.services.review_leave` | hr/services.py:99-128 | `LeaveRequestAdmin._review` | `is_own`, `balance_problems`, `working_days`, `record`, `notify`, `send_event`, `leave_approved_data` | `LeaveBalanceTests.test_approval_rechecks_the_balance`, `test_hr_manager_cannot_review_own_leave`, `test_cancelling_approved_leave_clears_attendance_and_restores_balance` |
| `hr.services.cancel_leave` | hr/services.py:132-147 | `LeaveRequestAdmin.cancel_selected` (+1) | `record`, `notify` | `test_cancelling_approved_leave_clears_attendance_and_restores_balance` |
| `hr.services.balance_problems` | hr/services.py:68-79 | `review_leave`, `validate_leave` | `leave_balances`, `leave_used` | `test_approval_rechecks_the_balance` |
| `hr.admin.LeaveRequestAdmin._review` | hr/admin.py:325-340 | `approve_selected` (343), `reject_selected` (347) | `is_hr_manager`, `services.review_leave` | `DepartmentHeadTests.test_head_approves_team_leave_but_not_own` (hr/tests.py:97-108) |
| `hr.admin.LeaveRequestAdmin.cancel_selected` | hr/admin.py:351-358 | admin action | `is_hr_manager`, `services.cancel_leave` | none found in the index |
| `hr.admin.is_hr_manager` | hr/admin.py | 20 callers (hotspot) | | many |
| `hr.models.LeaveRequest` | hr/models.py:114-141 | | | |
| `hr.tests.LeaveBalanceTests` | hr/tests.py:38-71 | | | |

## Notes
- Build (coder): Backend only (`hr/services.py`, `hr/admin.py` logic, `hr/tests.py`); no templates expected.
  Keep `is_hr_manager` where it is (20 callers); services may import it or a shared helper.
- Lock order: employee row first, then the request, everywhere, to avoid deadlocks between review and cancel.
- MySQL InnoDB honours `select_for_update`; SQLite ignores it, so the concurrency test is MySQL-only.
- Risk: outside calls (Make webhook, email via notify) inside the transaction would hold the lock during network
  time and fire even on rollback; that's why they move to `on_commit`.
- Decided by the user (Gate A, 2026-11-04): cancelling is limited to HR and the employee themselves (see acceptance criteria).
