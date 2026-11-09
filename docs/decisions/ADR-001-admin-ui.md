# ADR-001 — Django admin-only UI

**Date:** 2026-10-01  
**Status:** Accepted

## Context

STS Pulse needed a working LMS and HR system quickly for a small group of staff (≤ 50 users). A custom frontend would add weeks of HTML/CSS/JS work before any business logic could run. The user group is internal school staff, not the public.

## Decision

Use Django admin as the sole UI. All pages, forms and actions are built on top of `ModelAdmin`, `AdminSite`, custom `change_form` templates, and admin actions. No separate frontend framework, no REST API consumed by a SPA.

## Consequences

**Good**
- Instant CRUD, search, filtering, ordering and permissions for every model at zero extra code.
- Role-based access maps directly onto Django's built-in permissions system (`ModelAdmin.has_*_permission`).
- Customisation via `extra_style`, `change_form.html` overrides and CSS design tokens keeps the UI consistent without maintaining a component library.
- Audit log, approval workflows and inline formsets are Django-idiomatic and easy to test.

**Bad / accepted**
- Admin is not optimised for end users: URL patterns and page titles are fixed by Django.
- Mobile experience is acceptable but limited.
- Cannot expose a public API without adding DRF or separate views later.
- Any major UX redesign requires template overrides rather than component rewrites.
