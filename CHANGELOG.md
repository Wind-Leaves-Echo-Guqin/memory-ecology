# Changelog

## v2.1.0 (2026-09-03)

- **Core governance layer: zero functional change** — stable baseline since v2.0.0 (verified: all diffs vs production are line-ending and sanitization-only, e.g. generic topic lists and paths).
- **Versioning policy (this release)**: the open tree = the *core governance layer* (four gates, lifecycle state machine, search, health check, git gene-bank, versioning). Hermes-specific integration (session-history extraction, shell-hook injection, experience notebook) intentionally stays local — it reads Hermes runtime structures and is documented as the integration layer.
- **Local integration-layer evolution tracked here** (documentation only):
  - *Experience notebook*: pipeline added — signal detection (tool errors / user corrections) → LLM extraction → candidate area (`pending/backfill/`) → gate-reviewed adoption → keyword search / error-context retrieval → evidence registration (draft→verified).
  - *Error-context injection hook*: on tool failure, injects 1–3 related experiences into the agent context (progressive disclosure, echo-marked, fail-open, 15-min cooldown, three-state hit tracking).
  - *Historical backfill*: signal-density-driven batch extraction from high-value sessions (833 precise error signals; top-12 sessions = ~60%), with per-session checkpoint/resume and parallel queue execution.

## v2.0.0 (2026-09-01)

- **Phase 2 complete — full implementation open-sourced**: `src/memory_ecology/` (9 governance scripts + `lib/` common modules: config / fs / llm), zero-dependency clean-room test suite (`tests/`, mock LLM), `config.example.yaml`, publishing pipeline with pre-push sensitive-scan hook. The two Hermes-integration scripts (`eco_extract`, `eco_health_alert`) intentionally remain out of the open repo — they read Hermes runtime structures and are documented as the integration layer.
