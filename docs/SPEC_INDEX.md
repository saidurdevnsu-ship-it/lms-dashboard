# STS Pulse — documentation index

## Working documents (always up to date)

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | App structure, workspaces, roles, cross-cutting helpers, question-paper flow, HR flow, AI layer, configuration |
| [business-requirements.md](business-requirements.md) | Goals, features, role matrix, decisions taken |
| [database-design.md](database-design.md) | Every model, fields, constraints, indexes — derived from `models.py` |
| [development-workflow.md](development-workflow.md) | Local setup, commands, test procedure, commit/approval rules |
| [Api_Spec.md](Api_Spec.md) | URL catalog, custom admin views, request/response shapes |
| [Integration_Spec.md](Integration_Spec.md) | Make.com event schemas, mailer, document parsers, export engines |
| [security-review-2026-11-02.md](security-review-2026-11-02.md) | Security findings with current status |

## Decisions (ADRs)

| Decision | File |
|---|---|
| Django admin-only UI | [decisions/ADR-001-admin-ui.md](decisions/ADR-001-admin-ui.md) |
| threading.Thread for async work | [decisions/ADR-002-threading.md](decisions/ADR-002-threading.md) |
| Dual AI provider layer | [decisions/ADR-003-dual-ai-provider.md](decisions/ADR-003-dual-ai-provider.md) |
| Session-based workspace switching | [decisions/ADR-004-workspace-session.md](decisions/ADR-004-workspace-session.md) |
| Monolithic deployment | [decisions/ADR-005-monolith.md](decisions/ADR-005-monolith.md) |

## Agent pipeline artefacts

`docs/tasks/` — task files · `docs/reviews/` — code-review reports · `docs/test-reports/` — test reports

## Archived (superseded)

`docs/archive/` — formal spec suite written alongside the codebase; kept for history, not updated.
See [docs/archive/README.md](archive/README.md).
