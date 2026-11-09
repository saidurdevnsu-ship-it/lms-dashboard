# ADR-003 — Dual AI provider layer (Anthropic + Gemini)

**Date:** 2026-10-15  
**Status:** Accepted

## Context

The app started with the Anthropic Claude API for question generation and final-paper generation. API costs and regional availability led to adding Google Gemini as a second provider. Both providers need to be testable in isolation without changing call sites.

## Decision

All AI calls go through `questionbank.llm.ask` (internally `questionbank.ai._ask_claude` — the name is kept for both providers). The active provider and model are selected by env vars `AI_PROVIDER` (`anthropic` / `gemini`), `AI_MODEL`, `AI_FINAL_MODEL`, `GEMINI_API_KEY`, `GEMINI_PAID_TIER`. Tests blank all AI env vars and mock `ai._ask_claude` so no real provider is called.

## Consequences

**Good**
- Call sites (`generate_view`, `start_final_paper`, etc.) are unaware of which provider is active.
- Switching providers is a one-line `.env` change with no code change.
- Tests mock a single point (`ai._ask_claude`) regardless of provider.

**Bad / accepted**
- Provider-specific features (Claude's extended thinking, Gemini's grounding) are unavailable through the shared interface.
- Both providers' SDKs are installed, adding to `requirements.txt`.
- The name `_ask_claude` for a shared function is confusing; accepted to avoid a large rename.
- Prompt phrasing is tuned for Claude; Gemini output may differ in structure, requiring periodic re-testing when switching.
