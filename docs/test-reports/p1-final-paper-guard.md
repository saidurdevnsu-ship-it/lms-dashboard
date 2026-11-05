# Test report: p1-final-paper-guard

## Result: PASS
305 passed, 0 failed (MySQL, 0 skipped) · 305 passed, 0 failed, 3 skipped (SQLite)

## Runs

All commands from `C:\Users\User\Desktop\projects\lms\lms_dashboard` on branch `agent/p1-final-paper-guard`,
with the house test env prefix `env MAKE_WEBHOOK_URL= MAKE_MAILHOOK_ADDRESS= ANTHROPIC_API_KEY= EMAIL_HOST=
AI_PROVIDER= AI_MODEL= AI_FINAL_MODEL= GEMINI_API_KEY= GEMINI_PAID_TIER=` (abbreviated `<ENV>` below).
No other `manage.py test` python process was running before either MySQL run (checked with
`Get-CimInstance Win32_Process` -> 0, twice). The live `lms` database was never written to or migrated; the
dev server was left alone.

| # | Command | Backend | Result | Duration |
|---|---|---|---|---|
| 1 | `<ENV> venv/Scripts/python.exe manage.py test --noinput -v2` (baseline, before my tests) | MySQL 8.4 `test_lms` | Ran 288, OK, **0 skipped** | 112.9 s |
| 2 | `<ENV> DB_NAME= venv/Scripts/python.exe manage.py test --noinput -v2` (baseline) | SQLite | Ran 288, OK, 2 skipped | 162.5 s |
| 3 | `<ENV> venv/Scripts/python.exe manage.py check` | MySQL | System check identified no issues (1 silenced) | — |
| 4 | `<ENV> venv/Scripts/python.exe manage.py makemigrations --check --dry-run` | MySQL | No changes detected | — |
| 5 | probe module `questionbank.test_qa_probe` (25 scenario tests, deleted afterwards) | SQLite | Ran 25, OK, 1 skipped | 27.9 s |
| 6 | probe module `questionbank.test_qa_probe` | MySQL | Ran 25, OK, **0 skipped** | 25.2 s |
| 7 | `<ENV> DB_NAME= venv/Scripts/python.exe manage.py test questionbank --noinput -v2` (with the new tests) | SQLite | Ran 195, OK, 1 skipped | 59.4 s |
| 8 | `<ENV> venv/Scripts/python.exe manage.py test --noinput -v2` (final) | MySQL 8.4 `test_lms` | **Ran 305, OK, 0 skipped** | 114.4 s |
| 9 | `<ENV> DB_NAME= venv/Scripts/python.exe manage.py test --noinput -v2` (final) | SQLite | **Ran 305, OK, 3 skipped** | 102.8 s |
| 10 | `<ENV> venv/Scripts/python.exe manage.py test hr --noinput -v2` | MySQL | Ran 48, OK, **0 skipped** | 36.3 s |
| 11 | `manage.py check` / `makemigrations --check --dry-run` (final) | MySQL | clean / No changes detected | — |

Skips, backend by backend:
- MySQL ran **every** test: 0 skipped in runs 1, 6, 8 and 10. The two MySQL-only HR concurrency tests
  (`hr.tests.ConcurrentLeaveReviewTests.test_second_reviewer_of_the_same_request_waits_and_is_refused`,
  `...test_two_requests_over_the_balance_cannot_both_be_approved_at_once`) are confirmed present and `ok` in
  the MySQL log, and the new `questionbank.tests.ConcurrentFinalPaperClaimTests` ran there too.
- SQLite skipped exactly 3, all with an explicit reason: the 2 HR ones above ("SQLite serialises writes, so
  only MySQL can show two reviewers racing.") plus the 1 new final-paper claim race.
- 288 -> 305 is +17 tests, all added by QA. No test was weakened, skipped or deleted.

## Scenario matrix

Probed with the Django test client as real users (no live-DB writes, no real AI/Make/email calls; threads
patched and `_ask_claude` mocked as the house rules require). Two roles throughout: **Exam Controller**, a
*non-superuser* staff user in the `Exam Controller` group from `setup_academics` (so it only has
`manage_final_papers` + what the role really grants), and **Admin**, the superuser.

### Timing / ownership

| # | Scenario | Roles | Outcome |
|---|---|---|---|
| 1 | Full journey: open -> Regenerate -> page read-only while writing -> result arrives -> edit -> lock | Exam Controller (non-superuser), Admin | Correct for both. GET stays 200 while writing; `has_change_permission` and all four inline permissions go False; a crafted save POST is refused with "The AI is writing this paper. You can edit it once it's ready." and the questions are untouched; the run's result lands with a "Final paper ready" notification to its own user; the edit afterwards saves; the lock saves `status`/`locked_by`/`locked_at` and keeps the edited text. |
| 2a | **Run in flight, human edit refused** | both | Save POST redirects with the refusal message; question texts unchanged **and** `controller_notes` not written (the whole form is refused, not just the formset). `_regenerate`, `_regenerate_one` and `_lock` keep their own refusals in the same state ("already writing" / "still writing"). |
| 2b | **Run past the 15-min timeout, human edits, old result arrives** | Exam Controller | Edit saves and takes the paper back (`generation_started_at` -> NULL). The late result is discarded: 3 questions, **no duplicates**, the controller's text and notes intact, no `Stale question` rows. The only notification is "Final paper not generated… A newer request replaced this one; its result was not saved." — **no stray success notification** (asserted by diffing the notifications created after the lock/edit). |
| 2c | **Old run past timeout, human triggers a NEW run instead of editing** | Admin | The new claim replaces the token; the old run's result is discarded; the newer run keeps the paper (`is_generating` still True, `generation_error` still ""), then finishes normally. |
| 2d | **Regenerate-one racing a whole-paper regenerate** | both, both directions | The second request is refused with "The AI is already writing this paper." in both directions (whole-paper run blocks `_regenerate_one`; single-question run blocks `_regenerate`). A late single-question result arriving after a whole-paper rerun is discarded and question 2 keeps its current text. |
| 2e | **Lock while generating / from a stale page** | both | While writing: "The AI is still writing this paper. Lock it once it's ready.", status stays `generated`. From a page opened before someone else locked: "This final paper is already locked.", the first lock and its `locked_by` stand. From a page opened before a run started: `lock_final` re-reads and refuses. |
| 3 | Lock after the timeout, then the stuck run's result arrives | Exam Controller locks, Admin's zombie returns | The lock succeeds; the late result is refused by the LOCKED re-check **before** any delete, so the locked copy is byte-for-byte what was locked and no success notification is sent. (New case, not in the coder's tests.) |
| 4 | Legitimately slow run that nobody replaced (negative control) | Admin | Still saves its result and clears the flag with a "ready" notification. The take-back did **not** over-fix: only a run whose token was actually taken loses. |

### Confidentiality

| Scenario | Outcome |
|---|---|
| User involved as author / assignee / reviewer / admin-history editor, for **both** the non-superuser Exam Controller and the superuser Admin | Change page not 200; `_regenerate`, `_regenerate_one`, `_lock` POSTs all 404 via the excluded queryset with no thread started; print 403; export 403; the paper's title absent from the changelist; status and `generation_started_at` unchanged afterwards. Checked for all 4 ways of being involved × 2 users. |
| An involved user tries to use the **new save path** to take a stuck run's paper back | 404; the stuck run keeps its token and the questions are unchanged. The take-back is not a hole in `can_open_final`. |
| Teacher and HOD | 403 on the change page, changelist, print and export, 403 on a crafted `_regenerate` POST, and "final papers (confidential)" absent from the admin index. No final-paper note or question text anywhere. |

### Gemini free-tier gate

`AI_PROVIDER=gemini`, `GEMINI_PAID_TIER=False`, key patched in `os.environ`, SDK client patched (never called).
Refused at every entry point for both roles: `_generate_final` on the blueprint page, `_regenerate` and
`_regenerate_one` on the paper page, and `start_final_paper` / `start_question_regeneration` directly — each
with `FINAL_NOT_ALLOWED`, no thread started, no `genai.Client` constructed and no token left behind. **Also
refused immediately after a timeout take-back**, i.e. clearing the token does not open a door around the gate.

### Edge cases

| Case | Outcome |
|---|---|
| Timestamp exactly at `GENERATION_TIMEOUT` | `is_generating` is False exactly at the boundary (`<`, not `<=`), so the page becomes editable; at timeout − 30 s it is still read-only. Admin and inline permissions follow. |
| Microsecond-precision token comparison on MySQL | A value with `microsecond=123456` round-trips through `generation_started_at` unchanged on MySQL 8.4 (`DATETIME(6)`) and SQLite, and a token one microsecond off is rejected: the stale result is discarded and the real run keeps the paper. The two-connection race additionally asserts the stored token equals the winner's token to the microsecond. |
| Paper with 0 questions | Page opens 200, lock refused with "Generate the paper before locking it.", an ordinary save works and writes only `controller_notes`; no token written. |
| Ordinary save while **not** generating | Behaves exactly as before: no token written, `controller_notes` and the question text saved, "Edited after the automatic checks" note applied, and `status`, `model_used`, `generated_by`, `generated_at`, `check_summary`, `generation_error`, `locked_by`, `locked_at` all unchanged (the new `save_model` returns after one SELECT). |
| Bangla question text | Round-trips through the admin edit form (`text` and `answer`) and is rendered back on the page escaped-identical; a Bangla edit also survives a timeout take-back and the zombie result. |
| Two real connections claiming at once (MySQL only) | The second `start_final_paper` **waits** on the first one's `select_for_update` and is then refused with "The AI is already writing this paper."; exactly one token is stored and the loser records no `FINAL_STARTED` audit event. |

## Tests added

All in `questionbank/tests.py` (tests only; no app code was touched). 17 tests, +0 migrations.

`FinalPaperRoleTests` (new class, borrows `make_paper` / `one_question` / `stale_reply` / `token_of` /
`start` / `stall` / `paper_admin_and_inline` from the existing classes in the house style; adds a `post_form`
helper that can post as any user and change `controller_notes`):

| Test | What it proves |
|---|---|
| `test_exam_controller_without_superuser_walks_the_whole_paper` | The guard works for a real non-superuser role, not just the superuser the existing tests use: generate -> read-only -> result -> edit -> lock. |
| `test_admin_walks_the_whole_paper` | The same journey for the superuser. |
| `test_no_role_can_edit_or_race_a_run_in_flight` | For both roles: the save POST is refused, `controller_notes` is not written either, and all three buttons keep their refusals while a run holds the paper. |
| `test_a_single_question_run_also_blocks_a_whole_paper_run` | The shared flag works in the direction the existing tests didn't cover. |
| `test_a_slow_run_nobody_replaced_still_saves_its_result` | Guard against over-fixing finding 1: a run past the timeout that nobody took the paper from still saves and still notifies. |
| `test_locking_after_the_timeout_beats_the_stuck_runs_late_result` | A lock placed while a zombie still carries its token is never overwritten, and the zombie sends no success notification. |
| `test_lock_from_a_page_opened_before_someone_else_locked` | The second lock is refused and the first `locked_by` stands. |
| `test_an_involved_user_cannot_take_the_paper_back_by_saving` | The new `save_model` take-back respects `can_open_final`: an involved user gets 404 and the token survives. |
| `test_teachers_and_hods_see_no_final_paper_page_at_all` | Page, changelist, print, export, crafted POST and the admin index are all closed to Teacher and HOD. |
| `test_gemini_free_tier_still_refuses_after_a_timeout_take_back` | The paid-tier gate holds at every entry point *after* the token was cleared by an edit. |
| `test_the_timeout_boundary_decides_whether_the_page_is_read_only` | Exact `GENERATION_TIMEOUT` boundary behaviour of `is_generating` and the admin permissions. |
| `test_the_token_column_keeps_microseconds` | `generation_started_at` is sub-second precise on both backends — the assumption the whole design rests on. |
| `test_a_token_a_microsecond_apart_does_not_own_the_paper` | `_check_owns_run` is an exact comparison, not a tolerance. |
| `test_a_paper_with_no_questions_can_be_opened_and_saved_but_not_locked` | The 0-question paper behaves sensibly on every path. |
| `test_an_ordinary_save_writes_no_token_and_leaves_the_other_fields_alone` | Regression guard: the new `save_model` changes nothing for the ordinary save. |
| `test_bangla_question_text_survives_an_edit_and_a_take_back` | Bangla round-trips through the admin form, the page and a take-back. |

`ConcurrentFinalPaperClaimTests` (new `TransactionTestCase`, `@skipUnless(connection.vendor == "mysql")`,
same two-connection pattern as `hr.tests.ConcurrentLeaveReviewTests`):

| Test | What it proves |
|---|---|
| `test_only_one_of_two_simultaneous_runs_claims_the_paper` | Two real connections racing for the paper: the second **waits** for the row lock, is then refused with "already writing", exactly one token (microsecond-exact) is stored, and the loser writes no `FINAL_STARTED` audit event. The review argued this; now it is executed on MySQL. |

## Failures

None. No code bug was found, so nothing was reported back to the coder and no app file was changed.

Three observations that are **not** failures, recorded for the record:
- The narrow race the reviewer logged as finding 3 (a run claiming the paper between `changeform_view`'s
  `is_generating` check and `save_model`, milliseconds apart in one request) is now resolved in the
  controller's favour: the save clears the brand-new run's token and that run's result is later discarded with
  `STALE_RUN_MESSAGE`. No corruption either way, and the window is unchanged in size; the notification the
  losing run sends ("A newer request replaced this one") is technically accurate. Not reachable in a test
  without instrumenting the request; left as the reviewer left it.
- A discarded stale run still writes nothing into `generation_error`, so the page shows no trace of it (the
  run's own user is notified and the audit event is recorded). This matches the reviewer's open question 2 and
  looks right; it is a product decision for the architect, not a defect.
- A paper can end up locked while `generation_started_at` still holds an abandoned run's token. Harmless —
  `is_generating` is already False past the timeout and every write path re-checks LOCKED first — and now
  covered by `test_locking_after_the_timeout_beats_the_stuck_runs_late_result`.

## Not tested

- Real AI calls: every Anthropic/Gemini call is mocked, so the real provider timeout, retry and token-budget
  behaviour of a genuinely 15-minute run is unverified by design.
- Real browser: all role probing went through the Django test client, so JavaScript, the disabled-button
  rendering and the "AI is writing" panel were checked only as HTML assertions, not in a browser.
- True thread-level interleaving inside one run: the background thread is patched out and the job is called
  synchronously (the house pattern). The only genuine two-connection concurrency test is the new MySQL claim
  race; a race *inside* `generate_final_paper`'s `select_for_update` block was not forced.
- The live `lms` database and the running dev server: untouched on purpose. Nothing here proves behaviour
  against production data volumes.
- The sub-millisecond window of review finding 3 (claim landing between the permission check and the save).
- Load/performance of the final-paper page and export with large papers.
