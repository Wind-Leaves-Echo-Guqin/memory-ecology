# Memory Ecology

**A lifecycle approach to agent memory: memory is not a warehouse, it is an ecosystem.**

Most agent-memory systems today are *engines* — they focus on writing, retrieving, and offloading
memories (TencentDB Agent Memory, mem0, Zep, Letta, MemOS, …). This project takes a different
position: the hard problem is not storing more, it is **governance** — birth, life, death, and
evolution of memories in a single-user, low-maintenance environment.

> 记忆不是库存，是生态。—— Memory is not a warehouse, it is an ecosystem.

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

## Design principles (summary)

1. Evolution is the goal; management is the means.
2. User maintenance burden = **zero** (annual confirmations skippable, reports ignorable).
3. Rule-driven + conservative thresholds + reversible fallbacks; automation ≠ statistical calibration.
4. Every change reversible: git snapshots + operation logs + quarantine.
5. Mechanisms over protocols: anything proven unreliable by hand is mechanized.
6. Composition over accumulation; ecology over stacking.
7. Protected zones: core / buffer / experimental — immune to structural actions, not content updates.

Full details: [`docs/philosophy.md`](docs/philosophy.md)

## Repository contents

| Path | Content |
|---|---|
| [`docs/philosophy.md`](docs/philosophy.md) | The nine design principles and their reasoning |
| [`docs/architecture.md`](docs/architecture.md) | The four gates, two-axis state machine, protected zones, evaluation |
| [`docs/decisions.md`](docs/decisions.md) | Key design decisions — what was chosen, what was rejected, why |
| [`docs/lessons.md`](docs/lessons.md) | Field lessons from running this in production for a single user |

## Roadmap

- [x] **Phase 1 — Methodology (this repository)**: design philosophy, architecture, decisions, and lessons as open documentation.
- [x] **Phase 2 — Full implementation open-sourced (2026-09-01)**: the complete runnable implementation is now in this repository — `src/memory_ecology/` (9 governance scripts + `lib/` common modules: config / fs / llm), a zero-dependency clean-room test suite (`tests/`, mock LLM, runs anywhere), `config.example.yaml`, and a publishing pipeline that generates the open-source tree from the production codebase (personal identifiers stripped, `scripts/scan_sensitive.py` enforced by a pre-push hook). The two Hermes-integration scripts (`eco_extract`, `eco_health_alert`) intentionally remain out of the open repo — they read Hermes runtime structures and are documented as the integration layer. **Install-and-run is the acceptance bar**: `git clone → python -m unittest discover tests → green`.
- [x] **v2.1.0 (2026-09-03)**: core governance layer verified stable (zero functional drift); Hermes-integration evolution (experience notebook, error-context injection, historical backfill) tracked in `CHANGELOG.md` — this tree remains the governance-layer baseline. Versioning policy: open tree = core layer; integration layer stays local by design.

## Acknowledgments & references

- Design discussions were informed by: [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory), mem0, Zep/Graphiti, Letta (MemGPT), LangMem, Hindsight, OpenViking, Karpathy's LLM Wiki.
- Built and validated on [Hermes Agent](https://hermes-agent.nousresearch.com/) (Nous Research).

## License

MIT © Wind-Leaves-Echo-Guqin
