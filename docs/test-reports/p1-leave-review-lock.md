# Test report: p1-leave-review-lock
## Result: PASS
280 passed, 0 failed (MySQL: 280 run, 0 skipped; SQLite: 278 passed + 2 skipped, the MySQL-only concurrency tests)

## Runs
All commands from the project root with the env prefix
`env MAKE_WEBHOOK_URL= MAKE_MAILHOOK_ADDRESS= ANTHROPIC_API_KEY= EMAIL_HOST= AI_PROVIDER= AI_MODEL= AI_FINAL_MODEL= GEMINI_API_KEY= GEMINI_PAID_TIER=`
(`DB_NAME=` added for SQLite). No other `manage.py test` process was running before the MySQL runs (count 0).

| Command | Backend | Result | Duration |
|---|---|---|---|
| `venv/Scripts/python.exe manage.py test --noinput` (before QA tests were added) | MySQL (`test_lms`) | Ran 269, OK, 0 skipped | 184 s |
| `venv/Scripts/python.exe manage.py test hr.tests.LeaveRoleEdgeCaseTests --noinput` | SQLite | Ran 11, OK | 27 s |
| `venv/Scripts/python.exe manage.py test --noinput` (final) | MySQL | Ran 280, OK, 0 skipped | 231 s |
| `DB_NAME= venv/Scripts/python.exe manage.py test --noinput` (final) | SQLite | Ran 280, OK (skipped=2) | 191 s |
| `venv/Scripts/python.exe manage.py test hr --noinput -v 2` | MySQL | Ran 48, OK, 0 skipped | 82 s |
| `venv/Scripts/python.exe manage.py test hr.tests.ConcurrentLeaveReviewTests --noinput -v 2` x3 | MySQL | Ran 2, OK each time | 16 s / 17 s / 16 s |
| `venv/Scripts/python.exe manage.py check` | MySQL settings | No issues (1 silenced) | - |
| `venv/Scripts/python.exe manage.py makemigrations --check --dry-run` | - | No changes detected | - |

## Concurrency tests on MySQL
They ran and were not skipped. From the `hr -v 2` run on `test_lms`:
```
test_second_reviewer_of_the_same_request_waits_and_is_refused (hr.tests.ConcurrentLeaveReviewTests...) ... ok
test_two_requests_over_the_balance_cannot_both_be_approved_at_once (hr.tests.ConcurrentLeaveReviewTests...) ... ok
```
They also passed in both full MySQL runs and in 3 more separate runs (each 2/2 OK, about 5.8 s of test time per run).
I saw no flakiness. On SQLite both are skipped, as the contract says they should be.

## Tests added
New class `hr.tests.LeaveRoleEdgeCaseTests` in `hr/tests.py` (11 tests; I also added the `Holiday` and `escape` imports). Test code only, no app code changed.
- `test_hr_mixed_approval_approves_what_fits_and_names_each_refusal`: HR approves a selection holding a team request, HR's own request and an over-balance request. Only the team request is approved. Each refusal gets its own "Not approved: ..." message (self-review, balance), the summary reads "Approved 1", and exactly one Make event and one audit entry are recorded.
- `test_head_approves_team_only_and_other_departments_are_untouched`: a department head selects team, own and other-department requests. Only the team request is approved, own gets the self-review message, and the other department's request stays pending (`can_review_leave` is False).
- `test_head_rejection_notifies_the_employee_but_sends_no_make_event`: a rejection sends no `send_event` and creates no attendance. It sends one WARNING "Leave rejected" notification after commit.
- `test_plain_employee_cannot_review_in_the_admin_or_the_service`: an employee who is neither HR nor a head gets no approve action in the changelist. Forcing approve/reject posts changes nothing, the service refuses both a colleague's request and their own, and no audit entry is written.
- `test_employee_cannot_cancel_a_colleagues_leave`: the admin post and the service both refuse, and nothing is audited.
- `test_employee_mixed_cancel_withdraws_pending_and_names_the_approved_refusal`: with own pending + own approved selected, the pending one is cancelled. The approved one is reported with "Approved leave can only be cancelled by HR" and its attendance stays.
- `test_employees_stale_pending_copy_cannot_cancel_after_hr_approved`: the old page copy still says "may cancel", but the re-check under the lock refuses. Leave stays approved, its attendance stays, and nothing is audited.
- `test_request_exactly_at_the_balance_is_approved_and_one_more_day_is_refused`: 5 of 5 days are approved, then a 1-day request is refused with "Only 0 day(s) ... needs 1." and no extra attendance is created.
- `test_single_request_one_day_over_the_balance_is_refused`: a 6-day request against 5 days is refused, and rejecting the same request still works.
- `test_weekend_and_holiday_inside_the_range_are_neither_counted_nor_marked`: a Sun 1 to Sun 8 Nov range with a holiday on the 8th counts 5 days, so approval at exactly the balance works. Attendance is marked only on Nov 1-5 (no Fri/Sat, no holiday).
- `test_leave_over_new_year_is_checked_against_each_years_balance`: Dec 29 2026 to Jan 3 2027 is refused when only 1 day of 2026 is left (the message names 2026 only). It is approved after HR cancels the November leave: 3 days used in 2026, 1 in 2027, and attendance on the 4 working days.

## Failures
None. No code bugs found. The reviewer's Low/Info findings still stand and none of them fails a test:
- Change-form save without the lock (review finding 1, hr/admin.py:307-313)
- Silent skip of non-pending rows in actions (finding 2)
- `on_commit` without `robust=True` (finding 3)
- Dependence on READ COMMITTED (finding 4)
- Concurrency-test diagnostics (finding 5)

## Not tested
- The change-form race (review finding 1): it needs a race inside a single HTTP request. It is a follow-up candidate.
- A notification failure inside `on_commit` (review finding 3) and behaviour under REPEATABLE READ isolation.
- Real Make webhook, email or AI calls (all blanked or mocked by design), and UI checks in a real browser.
- Cancel racing review on MySQL across two connections. Only review/review is raced; cancel/review uses the same `lock_leave` order and is covered sequentially by the stale-copy tests.
