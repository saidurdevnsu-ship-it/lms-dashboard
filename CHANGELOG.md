# Changelog

All notable changes to STS Pulse. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added
- Academic dashboard redesign: greeting hero, workspace cards, Today panel (guarded by employee record), Attention, Coming Up, Quick Actions, Recent Actions partials
- Outlook-style Holiday add/change form (`templates/admin/hr/holiday/change_form.html`)
- Circle punch-in/out Attendance add/change form (`templates/admin/hr/attendance/change_form.html`)
- `admin-components.css` globally-loaded shared component library
- `learning/dashboard_service.py` — service layer extracted from dashboard_tags.py
- Dashboard template split into `templates/admin/dashboard/` partials

### Changed
- `admin-dashboard.css` thinned to layout-only; shared components moved to `admin-components.css`

### Docs
- Dual-doc problem resolved: formal spec suite moved to `docs/archive/`
- `docs/SPEC_INDEX.md` rewritten as a navigation index
- `docs/security-review-2026-11-02.md` — added Status column and tracking header
- `docs/database-design.md` — Mermaid ERD added
- `docs/decisions/` created with ADR-001 through ADR-005
- `docs/deployment.md` created
- `docs/archive/README.md` created

---

## [2026-10-xx] — PR #3: p1-final-paper-guard

### Fixed
- SR-15 / SR-CODE-07 / SR-CODE-08 / SR-DB-06: final paper generation guard and concurrency fixes
  - `select_for_update` claim on `FinalPaper` prevents double generation
  - 15-minute `GENERATION_TIMEOUT` lets a stuck run recover
  - Generation started/locked timestamps added
  - Final paper questions never exposed in notifications or audit log

---

## [2026-10-xx] — PR #2: p1-leave-review-lock

### Fixed
- SR-15: department heads could cancel team members' pending leave — now limited to own requests unless HR Manager
- Leave review locked once approved; only HR and the requesting employee may cancel

---
