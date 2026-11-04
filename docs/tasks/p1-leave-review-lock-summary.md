# Summary: p1-leave-review-lock

## What changed
- Approving, rejecting and cancelling leave now run in one database transaction that locks the employee and then
  the request, re-reads it, and refuses with a plain message if someone else already handled it
  ("This request was already approved."). The leave balance is checked while the lock is held.
- Who may review or cancel is decided in `hr/services.py`, not only in the admin:
  - review: HR managers, or the head of the employee's department; never your own request;
  - cancel (decided by the user, closes security finding SR-15): the employee may withdraw only their own
    pending request; HR may cancel pending or approved requests; department heads can no longer cancel their
    team's leave.
- The admin bulk actions report every refused row with its reason.
- Notifications and the Make `leave.approved` event are sent only after the change is saved; nothing is sent for
  a refused or rolled-back change. Audit entries are written inside the transaction.
- `is_hr_manager` moved to the new `hr/roles.py`; `hr.admin.is_hr_manager` still works for all 20 callers.

## Files
- `hr/services.py`, `hr/admin.py`, `hr/tests.py`, new `hr/roles.py`
- `docs/tasks/p1-leave-review-lock.md`, `docs/reviews/p1-leave-review-lock.md`,
  `docs/test-reports/p1-leave-review-lock.md`, this summary
- No migration, no template changes.

## Test results
- MySQL: 280 tests, OK (the two concurrency tests ran; QA repeated them 3 more times, all passed).
- SQLite: 280 tests, OK (2 skipped: the MySQL-only concurrency tests).
- `manage.py check` clean; `makemigrations --check` no changes.
- 29 new tests (18 by the coder, 11 by QA). Before the change: 251.

## Review
APPROVED in round 1, no Critical/High/Medium findings. Low follow-ups (not in this task):
1. The HR change form saves the request without the lock: a save at the same moment as an approval could set it
   back to pending.
2. Bulk actions skip non-pending rows without saying how many.
3. A failing notification after commit can stop a bulk action midway (`on_commit(..., robust=True)`).

## Check by hand
- As a department head: approve a team member's leave (works), try to cancel it (refused with a message).
- As an employee: withdraw your own pending request (works); try to cancel your approved leave (refused).
- As HR: cancel an approved leave and confirm the "on leave" days disappear from attendance.
