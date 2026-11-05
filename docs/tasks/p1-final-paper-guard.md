# Task: p1-final-paper-guard

## Goal
Make sure a confidential final paper can't be corrupted while the AI writes it: only the generation run that
currently owns the paper may save its result or clear the "being written" state, staff can't edit the paper
while the AI is writing it, and locking always saves the current copy. (Review findings CODE-07, CODE-08, DB-06.)

## Acceptance criteria
- [ ] **Run ownership (CODE-07).** Each generation run (whole paper or one question) is identified by the
      `generation_started_at` value it set in `_claim` (no new field, no migration). The background job passes
      that value along; when saving results (`generate_final_paper`, `regenerate_final_question`) it re-reads the
      paper under `select_for_update` and saves only if `generation_started_at` still equals its own value.
      Otherwise it discards its result and raises/records a plain message ("A newer request replaced this one;
      its result was not saved.").
- [ ] `_run_and_report` clears `generation_started_at` / sets `generation_error` only for its own run
      (`filter(pk=..., generation_started_at=<own value>)`), so a slow old run finishing after the 15-minute
      timeout can't clear or overwrite a newer run. Its notification still goes to its own user and says
      what happened.
- [ ] **No edits while the AI writes (CODE-08).** While `paper.is_generating`, the final-paper page is read-only:
      `has_change_permission(request, obj)` is False for that paper (questions inline and controller notes not
      editable) and a direct POST that would save the form or formset is refused with "The AI is writing this
      paper. You can edit it once it's ready." The regenerate/lock buttons keep their existing refusals.
      Viewing stays allowed.
- [ ] **Lock saves the current copy (DB-06).** `lock_final` runs in a transaction, re-reads the paper with
      `select_for_update`, re-checks locked / generating / no questions / blueprint problems on that fresh copy,
      and saves only `status`, `locked_by`, `locked_at` (`update_fields`). Audit and notifications only after a
      successful lock (notify via `transaction.on_commit`).
- [ ] Existing confidentiality rules unchanged: `can_open_final` / `INVOLVED_MESSAGE`, Gemini paid-tier guard,
      locked papers can't be regenerated.
- [ ] Tests: old run finishing after a newer run started doesn't overwrite questions and doesn't clear the newer
      flag; stuck run (past timeout) can be replaced and its late result is discarded; edit POST refused while
      generating (questions unchanged) and allowed afterwards; inline/notes read-only while generating; lock on a
      stale in-memory copy re-checks the fresh row (e.g. refuses if a run started meanwhile) and doesn't
      overwrite other fields; existing final-paper tests still pass.
- [ ] Full suite passes on MySQL and SQLite; `manage.py check` clean; `makemigrations --check` no changes.

## Out of scope
- The 3 low-risk leave follow-ups (separate small task).
- AI example language / MCQ option fixes (P1 item 4, next task).
- Moving question-bank generation to the background, timeouts of the AI call itself, UI redesign, wording
  changes other than the new refusal messages.

## Code map
<Filled in by the planner from the code index (Codebase Memory) or by reading the source. For orientation:
agents must read the current source before editing.>
Index: ready, indexed_at 2026-11-04T10:19:16Z, HEAD b95d29c (questionbank/ last changed 0af436f; no uncommitted edits)

| Symbol | File:lines | Callers | Calls into | Covering tests |
|---|---|---|---|---|
| `questionbank.ai._claim` | questionbank/ai.py:543-561 | `start_final_paper`, `start_question_regeneration` | `can_open_final`, `final_papers_allowed` | FinalPaperTests (tests.py ~200-260) |
| `questionbank.ai.start_final_paper` | questionbank/ai.py:564-576 | `QuestionBlueprintAdmin.changeform_view`, `FinalPaperAdmin.changeform_view` | `_claim`, `_in_thread` | FinalPaperTests |
| `questionbank.ai.start_question_regeneration` | questionbank/ai.py:579-592 | `FinalPaperAdmin.changeform_view` | `_claim`, `_in_thread` | `test_question_regeneration_runs_in_the_background_and_notifies` |
| `questionbank.ai._in_thread` | questionbank/ai.py:595-601 | start_* | `_run_and_report` | |
| `questionbank.ai._run_and_report` | questionbank/ai.py:604-634 | `_in_thread` (via write_final_paper / rewrite_final_question) | job, `record`, `notify`; clears flag at :625 unconditionally | `test_failed_background_generation_is_shown` |
| `questionbank.ai.generate_final_paper` | questionbank/ai.py:352-394 (save block 381-393) | `write_final_paper` + 18 (mostly tests) | `_ask_claude`, `check_final_questions`, `blueprint_slots` | QualityCheckTests, FinalPaperTests, test_llm |
| `questionbank.ai.regenerate_final_question` | questionbank/ai.py:476-540 (atomic block ~533) | `rewrite_final_question` | `_ask_claude` | regeneration tests |
| `questionbank.ai.write_final_paper` / `rewrite_final_question` | questionbank/ai.py:637-646 | `_in_thread` jobs | the two above | |
| `questionbank.workflow.lock_final` | questionbank/workflow.py:161-181 | `FinalPaperAdmin.changeform_view` (+ tests) | `paper_problems`, `record`, `notify` | lock tests (7 callers incl. tests) |
| `questionbank.models.FinalPaper` (+ `is_generating`, `GENERATION_TIMEOUT` 15 min) | questionbank/models.py:303-348 | property: index shows no callers; grep: ai.py:558, workflow.py:164, 2 templates, tests | | |
| `questionbank.admin.FinalPaperAdmin` | questionbank/admin.py:573-712 | | | |
| `FinalPaperAdmin.changeform_view` | questionbank/admin.py:639-668 | admin | `lock_final`, `start_*`, `claude_status`, `paper_problems` | page tests |
| `FinalPaperAdmin.save_formset` | questionbank/admin.py:591-600 | admin | | edited-question note tests |
| `FinalQuestionInline._editable` | questionbank/admin.py:555-557 | inline permissions | `can_open_final` | |

## Notes
- Build (coder): Backend first (`questionbank/ai.py`, `questionbank/workflow.py`, `questionbank/admin.py` logic,
  tests in `questionbank/tests.py`); Frontend only if the read-only note on the final-paper page needs template
  text (`templates/admin/questionbank/finalpaper/change_form.html` already shows "The AI is writing…").
- Compare timestamps exactly (same value round-tripped through the DB; MySQL DATETIME(6) keeps microseconds).
- Threads in tests: existing tests run `_in_thread` synchronously or patch it; follow their pattern.
- No migration expected (the token is the existing `generation_started_at`).
- Decisions taken as defaults (override at Gate A): the 15-minute stuck-run timeout stays; while generating the
  whole paper page is read-only, not just the questions.
