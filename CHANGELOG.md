# Changelog

## v3.1.0 (2026-10-25)

Core sync from the compatible line — the open core catches up with `memory-ecology-compat` v3.1.0 (previous sync: v2.2.4-era, commit `bf11d00`). Same recipe as before: gate scripts + `lib` mirror the compat production tree; compat-only surfaces (retrieval `memory_query`, vector cache, experience notebook, backup/restore, GUI) stay in the compat repo.

- **Entry discipline**: new `lib/cli_base.py` — shared CLI bootstrap (encoding fallback / data-root resolution / dirs / mutual exclusion / failure exits) wired into the gate CLIs. Fixes "data written but exit 1" on GBK consoles, gate ② lock files that never created parent dirs, and gate ④ computing its own data root while ignoring `MEMORY_ECOLOGY_ROOT`.
- **Concurrency & Windows hardening**: `gatekit` no longer reclaims a lock held by the same process (multi-agent re-entry now mutual-excludes); `fs.atomic_write` gains retry/rollback on Windows when a reader holds the target; BOM-tolerant reads (`utf-8-sig`) stop silently dropping the first pending candidate.
- **Privacy**: new `lib/pii.py` — PII detection/redaction applied to write-gate ledger lines and the quota EXTRUDE ledger (same caliber on both gates).
- **Similarity & contracts**: similarity logic now single-sourced in new `lib/similarity.py`; `lib/contracts.py` folded away (no importers remain).
- **Version single source**: `lib/__init__.py` carries `__version__ = "3.1.0"` — gate startup banners and releases read this instead of four divergent version constants.
- **LLM endpoint configurable**: `lib/llm.py` resolves base_url/model from config/env instead of hardcoding; rule-tie fallback downgraded to DISPUTE (false positives 43% → 0); body caps configurable with visible truncation marks; config Guard for the v3.1 back-to-source-text / scope work.
- Validation: `compileall` clean; script-mode import smoke of all 9 gate scripts OK; `tests/test_cleanroom.py` 4/4 green.

## v2.2.0 (2026-09-06)

Safe write-path hardening (the v2.2 first item) + evaluation gate lines:

- **Safe write path**: new `lib/safeio.py` — entry-name whitelist + traversal guard, `detail`-entry frontmatter schema validation (type/status enums, ISO dates, non-negative counters), atomic writes with optional backups, JSONL append with size caps. Wired into every entry writer (`write_gate` ADD/UPDATE/supersede).
- **Evaluation gate lines** (stage C of the versioning strategy): `eco_eval` adds two gate lines — **INJ** (injections over the 1500-char budget ≤ 5 / 30 days) and **GOLD** (pseudo-gold = memories actually used, top-5 replay relevance ≥ 60%; requires the experience-notebook retriever, reported as SKIP on pure-memory installs); `--gate` rule-only mode; INSUFFICIENT is reported honestly and never silently counted as PASS/FAIL.
- **`scripts/scan_sensitive.py` removed from the repo**: the sanitizer's own blocklist enumerated private identifiers (a meta-leak — the censor's wordlist reveals what it protects). Sanitization now runs exclusively in the private publishing pipeline; the shipped tree is its verified output.
- Dependency note: `lib/safeio.py` ships with the tree; `eco_eval` degrades gracefully when the optional experience-notebook retriever is absent.

## v2.1.1 (2026-09-05)

Bugfix + regression hardening (open-tree touch = one threshold constant in `eco_health_check.py`):

- **Extraction robustness (5 defects fixed)**: greedy `[.*]` regex replaced with balanced-bracket extraction; `exit_code: true/false` no longer treated as error (bool is int subclass); JSON-array tool content parsed recursively; pytest title-style `FAILED tests/...` (no colon) detected; empty-context signal skip now logged.
- **Memory threshold alignment**: health check 2200 → 2550 chars (matches quota management line; eliminates false positives in the 2200–2550 band).
- **Pipeline safety**: backfill queue lock (O_EXCL + stale reclaim) guards against double-executor runs; rank aggregation folds same-episode entries (prevents injecting the same event multiple times).
- **Merge-candidate list generator** (read-only): near-duplicate pairs (n-gram similarity ≥ 0.75) from production entries + candidate pool, with keep/absorb suggestion — human check required before any merge.
- **Regression hardening**: persistent gate test suite (write_gate / eco_quota / distill_stage / eco_review) replacing ad-hoc validation scripts; all five extraction defects fixed as fixtures.

## v2.1.0 (2026-09-03)

- **Core governance layer: zero functional change** — stable baseline since v2.0.0 (verified: all diffs vs production are line-ending and sanitization-only, e.g. generic topic lists and paths).
- **Versioning policy (this release)**: the open tree = the *core governance layer* (four gates, lifecycle state machine, search, health check, git gene-bank, versioning). Hermes-specific integration (session-history extraction, shell-hook injection, experience notebook) intentionally stays local — it reads Hermes runtime structures and is documented as the integration layer.
- **Local integration-layer evolution tracked here** (documentation only):
  - *Experience notebook*: pipeline added — signal detection (tool errors / user corrections) → LLM extraction → candidate area (`pending/backfill/`) → gate-reviewed adoption → keyword search / error-context retrieval → evidence registration (draft→verified).
  - *Error-context injection hook*: on tool failure, injects 1–3 related experiences into the agent context (progressive disclosure, echo-marked, fail-open, 15-min cooldown, three-state hit tracking).
  - *Historical backfill*: signal-density-driven batch extraction from high-value sessions (833 precise error signals; top-12 sessions = ~60%), with per-session checkpoint/resume and parallel queue execution.

## v2.0.0 (2026-09-01)

- **Phase 2 complete — full implementation open-sourced**: `src/memory_ecology/` (9 governance scripts + `lib/` common modules: config / fs / llm), zero-dependency clean-room test suite (`tests/`, mock LLM), `config.example.yaml`, publishing pipeline with pre-push sensitive-scan hook. The two Hermes-integration scripts (`eco_extract`, `eco_health_alert`) intentionally remain out of the open repo — they read Hermes runtime structures and are documented as the integration layer.
