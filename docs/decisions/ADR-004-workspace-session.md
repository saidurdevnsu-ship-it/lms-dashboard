# ADR-004 — Session-based workspace switching

**Date:** 2026-10-01  
**Status:** Accepted

## Context

Staff may belong to both the Academic and HR workspaces (e.g. a teacher who is also an employee). The two workspaces have distinct navigation, permissions and dashboard widgets. Showing both simultaneously creates a cluttered admin index and nav.

## Decision

The active workspace is stored in `request.session['workspace']` (`'academic'` or `'hr'`). `learning/views.py` `switch_workspace` changes it on GET. `base_site.html` reads the session key to show the correct nav and dashboard. The dashboard service (`learning/dashboard_service.py`) gates widgets on the active workspace and the user's group membership.

## Consequences

**Good**
- Users see a clean, focused interface for the task at hand.
- No extra model or DB column: the session is already present for auth.
- Switching is instant (no page reload of data, just a session write and redirect).

**Bad / accepted**
- State-change on GET is a known CSRF-class issue (SR-16, accepted risk — impact minimal).
- If a user's session expires mid-task, they land in the default workspace on next login.
- Tests that exercise both workspaces must explicitly set `session['workspace']` in the test client.
