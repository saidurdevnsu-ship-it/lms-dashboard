# ADR-002 — threading.Thread for async work

**Date:** 2026-10-01  
**Status:** Accepted

## Context

Final-paper generation and question regeneration call the AI provider and can take 30–120 seconds. Running them in the WSGI request would block a worker for that duration, timing out the browser and exhausting the worker pool under concurrent requests. A proper task queue (Celery, Django 6 Tasks backend, etc.) requires a broker, additional processes to run and manage, and deploy complexity.

## Decision

Long-running work (AI generation, email dispatch) runs in `threading.Thread` launched from the view. The DB-level `generation_started_at` + 15-minute `GENERATION_TIMEOUT` claim (`questionbank/ai.py` `_claim`) prevents double-runs and lets a stuck job recover. Views return immediately; the client polls or receives an in-app notification on completion.

## Consequences

**Good**
- Zero extra infrastructure: no Redis, RabbitMQ, Celery workers or supervisord.
- Simple to test: `patch('questionbank.ai.threading.Thread')` in every test that might trigger generation.
- Adequate throughput for ≤ 50 users with infrequent generation.

**Bad / accepted**
- Threads are daemon threads inside the web process: an unclean restart loses any in-flight generation. The DB claim allows the next request to retry after the timeout.
- Thread errors are caught and stored in `FinalPaper.generation_error`; there is no central task queue dashboard.
- Does not scale horizontally: two web processes could both try to claim the same paper between the select and the update (mitigated by `select_for_update`).
- **Trigger to migrate**: move to Celery or Django 6 Tasks before adding more than one WSGI worker or adding any scheduled background job.
