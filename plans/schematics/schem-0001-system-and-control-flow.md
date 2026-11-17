---
id: SCHEM-0001
title: Vharness Next system and control flow
type: schematic
status: current
owners: []
created: 2026-09-04
updated: 2026-11-16
depends_on: [ARCH-0001, ADR-0001, ADR-0002, ADR-0003, ADR-0004, ADR-0005, ADR-0019]
supersedes: []
related: [ROAD-0001, PHASE-0001, PHASE-0002, PHASE-0003, PHASE-0004, ADR-0019]
---

# Vharness Next system and control flow

This cross-document view represents ARCH-0001, ADR-0001 through ADR-0005, and
ADR-0019, which supersedes ADR-0007.
Arrows crossing the runtime boundary are proposals and receipts, never direct
Vharness execution authority. The coordinator alone writes session state;
runtime records live in a distinct store owned by the runtime worker.

```mermaid
flowchart TB
    Human[Human operator] -->|messages and controls| UI[Interaction surface]
    UI --> Control[CLI control client]
    Control -->|local socket or pipe| Inbox[Coordinator transport inbox]
    Inbox --> Coordinator[Session coordinator]
    Coordinator --> Objective[Versioned objective]
    Coordinator --> Journal[(Session journal and projections)]
    Journal --> Projections[Rebuildable memory projections]
    Projections --> Context[Deterministic context assembler]
    Context --> Agent[General agent kernel]
    Coordinator -->|model decision request| Workers[Bounded blocking I/O workers]
    Workers --> Agent
    Agent -->|typed result| Workers
    Workers -->|model result| Inbox

    Journal --> Monitor[Progress monitor]
    Monitor -->|repetition or budget-checkpoint trigger and bounded digest| Coordinator
    Coordinator -->|supervisor request| Workers
    Workers --> Supervisor[Advisory supervisor]
    Supervisor -->|guidance; no tools| Workers
    Workers -->|guidance result| Inbox
    Journal --> Attempts[All variation attempts]
    Journal --> Lineage[Externally accepted single lineage]
    Attempts --> Promotion[Applicability and stale-head check]
    Promotion -->|current externally accepted evaluation only| Lineage

    Coordinator -->|authorized dispatch| Workers
    Workers -->|ActionProposal| Runtime[External controlled runtime]
    Runtime -->|ExecutionReceipt| Workers
    Runtime --> RuntimeStore[(Separate runtime execution store)]
    Workers -->|runtime result| Inbox
    Runtime --> Environment[External environment or target]
    Environment -->|Observation| Workers
    Workers -->|observation result| Inbox
    Coordinator -->|evaluation request| Workers
    Workers --> Evaluator[External evaluator]
    Evaluator -->|authoritative EvaluationReceipt| Workers
    Workers -->|evaluation result| Inbox
    Coordinator --> Promotion

    RuntimeBoundary{{identity / policy / credentials / isolation / execution}}
    Runtime --- RuntimeBoundary
    Journal -. sole session writer .- Coordinator
    ValidationBoundary{{lifecycle / authoritative validation and scoring}}
    Environment --- ValidationBoundary
    Evaluator --- ValidationBoundary
```

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Running: start
    Running --> Waiting: named external condition
    Waiting --> Running: condition resolved
    Running --> Paused: operator pause
    Paused --> Running: operator resume
    Waiting --> Paused: operator pause
    Paused --> Waiting: resume into unresolved wait
    Running --> Stopping: operator stop
    Paused --> Stopping: operator stop
    Waiting --> Stopping: operator stop
    Running --> Completed: externally supported completion
    Running --> Failed: unrecoverable internal integrity failure
    Stopping --> Stopped: checkpoint and settle
    Completed --> [*]
    Failed --> [*]
    Stopped --> [*]
```

## Notes and unresolved branches

The initial coordinator has one event writer, at most one unresolved
state-changing proposal per session, and one committed lineage. An attempt may
span many actions; only external acceptance advances lineage. Waiting names a
specific external condition and does not imply completion or new authority.
Workers may perform blocking model, supervisor, and runtime calls, but cannot
schedule or dispatch independently or write session state. CLI controls use
local IPC to the coordinator; in-process workers return through its bounded
inbox. Runtime storage is separate from the session database. Evaluation intake
may be retained while paused or stopped but is consumed only if applicable and
schedulable; Stopped remains terminal. Killing a process does not reverse its
effects. Population/archive branching is deferred. PHASE-0004 treats runtime
delivery and campaign validation as independent gates (ADR-0019), while all
four campaign families remain necessary for full verification.
