# Memory Ecology

**A lifecycle approach to agent memory: memory is not a warehouse, it is an ecosystem.**

`MIT` · `Python 3.10+` · `zero-dependency core` · `pure local` · `v3.1.0`

> 记忆不是库存，是生态。—— Memory is not a warehouse, it is an ecosystem.

Most agent-memory systems today are *engines* — they focus on writing, retrieving, and offloading
memories (TencentDB Agent Memory, mem0, Zep, Letta, MemOS, …). This project takes a different
position: the hard problem is not storing more, it is **governance** — birth, life, death, and
evolution of memories in a single-user, low-maintenance environment.

## TL;DR

- **What** — four lifecycle gates (write → consolidate → distill → review) keep agent memory
  bounded, alive, and reversible. Rule-driven, zero maintenance, LLM only adjudicates similarity
  conflicts (gate ①) and persona wording (gate ③); physical deletion is disabled at code level.
- **This repository** — the open **core governance layer**: methodology docs + the 9 gate scripts
  and shared `lib/` they run on.
- **Need host adapters?** — [memory-ecology-compat](https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology-compat)
  is the multi-host derivative (dsh adapters, retrieval consumer, experience notebook, local GUI).
  The two lines are version-aligned — both at **v3.1.0**; this tree's core is periodically mirrored
  from compat's production tree.

## What is different

| Aspect | Typical memory engine | This project (Memory Ecology) |
|---|---|---|
| Core question | How to store & recall more? | How to keep memory alive *and bounded* forever? |
| Lifecycle | None — memories accumulate | Two-axis state machine: activity (active↔dormant↔frozen) × value (retained↔superseded↔archived) |
| Evolution | Manual or roadmap | Rule-driven automation: co-occurrence → variant → evaluation → replace/coexist/archive |
| Deletion | Often hard-delete | **Never delete** — `superseded` marks + quarantine + git snapshots (everything reversible) |
| Maintenance | Operator-curated | **Zero user maintenance** — automatic by default, thresholds self-apply, user only checks results (optional) |
| Calibration | Statistical, needs data | **Rule-driven + conservative thresholds + reversible fallbacks** — works with zero statistical samples |
| Evaluation | Benchmarks on public datasets | Five-dimension health eval adapted to single-user reality (extraction / multi-session reasoning / knowledge update / temporal reasoning / safe abstention) |

## Architecture: four gates of the memory lifecycle

```
                 ┌────────────────────────────────────────────────┐
   session       │  GATE 1: Write Integration (per day)           │
   extraction ──▶│  type-typing + similarity check +              │
   candidates    │  ADD / UPDATE / NOOP / CONFLICT decision       │
                 │  + event clock (valid_time / transaction_time) │
                 └──────────────────────┬─────────────────────────┘
                                        ▼
   L2 detail store  ◀── demote ──  ┌──────────────────────────────┐
   (markdown files,                 │  GATE 2: Consolidation &     │
    frontmatter metadata)           │  Quota (daily)               │
    ▲                               │  promote: occurrences≥N &    │
    │ distill                        │  cross-session ⇒ L1 resident │
    │ (stable semantic facts)        │  evict: L1 > 85% quota ⇒ L2  │
    │                                │  (high-value items protected)│
    └───────────────────────────────└──────────────┬───────────────┘
                                                    ▼
   L1 resident (MEMORY.md / USER.md,  ──  GATE 3: Persona Distillation
    size-bounded, always injected)        stable semantic → USER traits
                                          30-day observation period
                                          synonym-replace, never overwrite blind
   ┌──────────────────────────────────────────────┐
   │  GATE 4: Review (monthly)                    │
   │  last_verified expiry → dormant → archived    │
   │  fragment-merge candidates, quarantine TTL    │
   └──────────────────────────────────────────────┘
```

Every gate is an **independent script** outside the protected core, every action is **logged and
reversible** (git snapshot of the whole memory tree daily), and physical deletion is **disabled at
the code level**.

## Quick start

```bash
git clone https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology.git
cd memory-ecology

# 1) clean-room test suite — mock LLM, zero dependencies, runs anywhere
python -m unittest discover tests

# 2) poke a gate (read-only search over the memory store)
cd src/memory_ecology
python eco_search.py --help

# 3) read the methodology before wiring anything
#    docs/philosophy.md → docs/architecture.md
```

Notes:

- The data root defaults to the install location; set `MEMORY_ECOLOGY_ROOT` for multi-instance or
  custom layouts. Thresholds come with conservative defaults — see `config.example.yaml`.
- Gates ① and ③ call an LLM (DeepSeek-compatible endpoint; key from `MEMORY_ECOLOGY_API_KEY` or
  `.env` in the data root, base URL configurable). Everything else is pure rules and runs offline.
- Host-specific wiring (session-history extraction, injection hooks) is the **integration layer** —
  see the compat repo's `integrations/` for reference adapters.

## Design principles (summary)

1. Evolution is the goal; management is the means.
2. User maintenance burden = **zero** (annual confirmations skippable, reports ignorable).
3. Rule-driven + conservative thresholds + reversible fallbacks; automation ≠ statistical calibration.
4. Every change reversible: git snapshots + operation logs + quarantine.
5. Mechanisms over protocols: anything proven unreliable by hand is mechanized.
6. Composition over accumulation; ecology over stacking.
7. Protected zones: core / buffer / experimental — immune to structural actions, not content updates.

Full details: [`docs/philosophy.md`](docs/philosophy.md)

## Repository layout

| Path | Content |
|---|---|
| [`docs/philosophy.md`](docs/philosophy.md) | The nine design principles and their reasoning |
| [`docs/architecture.md`](docs/architecture.md) | The four gates, two-axis state machine, protected zones, evaluation |
| [`docs/decisions.md`](docs/decisions.md) | Key design decisions — what was chosen, what was rejected, why |
| [`docs/lessons.md`](docs/lessons.md) | Field lessons from running this in production for a single user |
| `src/memory_ecology/` | The 9 governance scripts (gates, search, health check, eval, state) + shared `lib/` (config / fs / gatekit / llm / memstore / metrics / pii / similarity / cli_base / safeio / inject_gate) |
| [`tests/test_cleanroom.py`](tests/test_cleanroom.py) | Zero-dependency clean-room suite (mock LLM, fixture isolation) |
| [`config.example.yaml`](config.example.yaml) | Configuration template with conservative defaults |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |

## Two repositories, one lineage

| | This repo (`memory-ecology`) | [`memory-ecology-compat`](https://github.com/Wind-Leaves-Echo-Guqin/memory-ecology-compat) |
|---|---|---|
| Role | Core governance layer — methodology + the four gates' reference implementation | Multi-host derivative — portable core + host adapters |
| Extra surface | — | Retrieval consumer (`memory_query`), experience notebook, backup, local GUI, dsh / Hermes adapters |
| Versioning | Aligned with compat since v2.1.2 (currently **v3.1.0**) | Production tree; this repo's core mirrors it |

## Milestones

- **2026-08 · Methodology** — philosophy, architecture, decisions, lessons published as open docs.
- **2026-09-01 · v2.0.0** — full implementation open-sourced; install-and-run acceptance bar (`clone → tests → green`).
- **2026-09-06 · v2.2.0** — safe write-path hardening (`lib/safeio.py`) + evaluation gate lines (INJ / GOLD).
- **2026-10-25 · v3.1.0** — core synced with the compat production tree: entry discipline (`lib/cli_base.py`), concurrency & Windows atomic-write hardening, PII redaction (`lib/pii.py`), similarity single-source, version single-source.

Full history: [`CHANGELOG.md`](CHANGELOG.md)

## Acknowledgments & references

- Design discussions were informed by: [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory), mem0, Zep/Graphiti, Letta (MemGPT), LangMem, Hindsight, OpenViking, Karpathy's LLM Wiki.
- Built and validated on [Hermes Agent](https://hermes-agent.nousresearch.com/) (Nous Research).

## License

MIT © Wind-Leaves-Echo-Guqin
