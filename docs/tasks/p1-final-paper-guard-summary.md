# Task summary: p1-final-paper-guard

**Branch:** `agent/p1-final-paper-guard`  
**Merged:** PR #3  
**Commit:** `ccac453`

## What changed

- Added `select_for_update` DB claim on `FinalPaper` before starting generation, preventing two threads from generating the same paper concurrently (SR-CODE-07 / SR-DB-06).
- Added `generation_started_at` and `GENERATION_TIMEOUT` (15 minutes) so a stuck or crashed generation run is recoverable — the next request can re-claim after the timeout expires (SR-CODE-08).
- Hardened confidentiality: final-paper questions never appear in notifications, audit log entries or Make.com events (SR-15).
- Added `generation_error` field to `FinalPaper` so errors from background threads surface to the Exam Controller in the admin.

## Files touched

- `questionbank/models.py` — `FinalPaper`: `generation_started_at`, `generation_error`, `GENERATION_TIMEOUT`
- `questionbank/ai.py` — `_claim`, `start_final_paper`, `start_question_regeneration`
- `notifications/services.py` — guard against including final-paper content
- `audit/signals.py` — guard against logging final-paper questions
- `questionbank/migrations/` — migration for new fields

## Test results

All tests passed on both SQLite and MySQL backends (run with blanked AI, Make and email env vars).

## Manual checks for the user

1. Generate a final paper and immediately navigate away — confirm the paper completes in the background and the status updates to `generated`.
2. Try to generate the same paper twice by opening two browser tabs and clicking generate within seconds of each other — only one generation should run; the second should see "already generating".
3. Confirm that the approval notification for a blueprint does not include any final-paper question text.
