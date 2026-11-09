# ADR-005 — Monolithic deployment

**Date:** 2026-10-01  
**Status:** Accepted

## Context

STS Pulse serves a single school with ≤ 50 internal users. Microservices or separate services per domain (academic, HR, notifications) would add network overhead, service discovery, distributed tracing and independent deploy pipelines — none of which are needed at this scale.

## Decision

All Django apps (`learning`, `questionbank`, `hr`, `notifications`, `audit`) run in a single Django process, share one database (MySQL in production, SQLite for development/tests), and are deployed as one unit. The reverse proxy (nginx or equivalent) handles static files and media (`X-Accel-Redirect`). One `manage.py` controls all migrations, management commands and the dev server.

## Consequences

**Good**
- Single deploy, single process, single database connection pool.
- Cross-app queries (e.g. dashboard service joining HR and questionbank data) are plain ORM joins with no network hop.
- One test suite, one `requirements.txt`, one set of env vars.
- Easy to reason about: no distributed transaction problem.

**Bad / accepted**
- A bug in one app can crash the whole process.
- Cannot scale individual apps independently (not needed at ≤ 50 users).
- Moving to microservices later requires extracting DB tables, replacing in-process calls with API calls, and migrating auth.
- **Trigger to reconsider**: sustained load from multiple schools, or a need to deploy the HR module independently.
