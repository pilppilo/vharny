> This copy does not include the coordination tooling or the `coordination/`
> records that earlier workstream entries cite. Evidence bundles kept under
> `evidence/` are the ones the test suite loads. Other evidence paths named in these documents (the
> EXP-0002 and EXP-0003 bundles, CartPole, the P0 diagnostic, and
> orchestration-monitor runs) were not copied.

# Vharness Next planning index

Protocol: Plant v1.2 (`plans/PROTOCOL.md`)

Plant source revision: c4f886e71f96b0b15a226be241155a8573579e01

The protocol, schema, and templates were taken from that revision of the
independent Plant source project. Under ADR-0018 the Plant CLI is not used:
apply `plans/PROTOCOL.md` by hand. For current direction, read Current focus
below, the WORK-0001 Current handoff, and each Required heading in the Read
manifest. Treat Now and Read IDs as authoritative references, not mandatory
initial reads. Before expanding, name the specific gap or task condition,
unresolved question, and one exact file and heading.

## Current focus

- **Now:** PHASE-0004.
- **Read:** ADR-0019, ADR-0018, ADR-0008, ADR-0009, ARCH-0001, ROAD-0001, BENCH-0001, PHASE-0004, PHASE-0003.
- **Workstream:** WORK-0001.
- **Execution direction:** PHASE-0002 and PHASE-0003 are `verified`. PHASE-0004 is `implementing` under ADR-0019: its runtime delivery and campaign verification gates are both required for verification and are not sequential. Current work is the ARC-AGI-3 environment integration, then its BENCH-0001 campaign; a scored run needs its frozen campaign manifest first. The Timing Task 2 and Omarchy Task 1 acceptance runs for the runtime delivery gate are deferred by the operator (2026-11-16), not dropped: no run is staged, and resuming either needs the operator's word and fresh staging. All four campaign families remain required for full PHASE-0004 verification. A prepared host, provider credentials, tool setup, and selected HTB task are prerequisites only for their corresponding live acceptance runs. No merge into `next` is planned for the foreseeable future; any later merge and any default-entrypoint switch require separate future operator decisions.
- **Next:** None.
- **Blocked:** None.

### Read manifest

- **Required:** ADR-0019#Decision
- **Required:** ADR-0018#Decision
- **Required:** ADR-0008#Decision
- **Required:** ADR-0009#Decision
- **Required:** PHASE-0004#Entrance criteria
- **Required:** PHASE-0004#Implementation sequence
- **Required:** PHASE-0004#Exit criteria and required evidence
- **Required:** BENCH-0001#Required evidence

## ID reservations

None.

## Governing direction

| ID | Document | Status | Purpose |
| --- | --- | --- | --- |
| ARCH-0001 | `architecture/arch-0001-avo-inspired-agent-system.md` | accepted | Vharness Next architecture, contracts, state, algorithms, and boundaries |
| ROAD-0001 | `roadmaps/road-0001-vharness-next.md` | accepted | Ordered delivery of the redesign |
| SCHEM-0001 | `schematics/schem-0001-system-and-control-flow.md` | current | Searchable cross-document system and control-flow view |

## Decisions

| ID | Status | Decision | Supersedes |
| --- | --- | --- | --- |
| ADR-0001 | accepted | Build one AVO-inspired, domain-neutral agent core | — |
| ADR-0002 | accepted | Keep execution enforcement and authoritative validation outside Vharness | — |
| ADR-0003 | accepted | Use environment contracts, not benchmark-specific behavioral profiles | — |
| ADR-0004 | accepted | Make the human operator a durable, first-class participant | — |
| ADR-0005 | accepted | Use benchmark families as a capability engineering ladder | — |
| ADR-0006 | superseded | Use the Plant packet for current planning direction | — |
| ADR-0007 | superseded | Deliver the operator runtime before PHASE-0004 campaigns | — |
| ADR-0008 | accepted | Supervisor negative-signal families are a mandatory candidate menu; evidence fingerprint defined | — |
| ADR-0009 | accepted | The operator-built Timing recreation is the PHASE-0004 HTB acceptance target | — |
| ADR-0010 | accepted | Governed `completed` flips are verification-gated; committed oracles carry their computation recipe | — |
| ADR-0011 | superseded | Adopt `monitor.p5.v1` as the shipped default supervision policy; the kernel switch is a separately gated change | — |
| ADR-0012 | accepted | Recent-band content visibility: per-kind renderer, session-resolved receipt excerpts, repeat collapsing, derived self-state; ranking untouched | — |
| ADR-0013 | accepted | Bounded provider transport recovery with a durable operator-resumable waiting state; supported-progress budget checkpoints as the P6 monitor candidate | ADR-0011 |
| ADR-0014 | accepted | Adopt `monitor.p6.v1` as the shipped supervision default per EXP-0003; costs accepted; separately gated kernel switch | ADR-0011 |
| ADR-0015 | accepted | Decision decode repair ladder: observational-field recovery in place and operator-resumable malformed-output waits; compact pinned re-ask proposed for a later change | — |
| ADR-0016 | accepted | Provider token accounting: recorded counters keep their selection roles; provider-reported usage calibrates admission; over-budget projections are refused and breaches journaled | — |
| ADR-0017 | accepted | An empty, length-limited decode spends one repair-budget re-ask at the reserved ceiling, journaled as a repair note; a second exhaustion stays terminal | — |
| ADR-0018 | accepted | Apply the planning protocol by hand; the Plant CLI is retired and Current focus is the starting point for direction | ADR-0006 |
| ADR-0019 | accepted | PHASE-0004 gates are independent; ARC-AGI-3 work proceeds before the deferred Timing and Omarchy acceptance runs | ADR-0007 |

## Delivery order

| Order | ID | Status | Depends on | Outcome |
| --- | --- | --- | --- | --- |
| 1 | PHASE-0001 | verified | ARCH-0001, ROAD-0001, BENCH-0001, ADR-0001..0005 | Resumable session kernel, attempt history, committed lineage, and external contracts |
| 2 | PHASE-0002 | verified | PHASE-0001 | Autonomous variation loop, memory, context, and live human interaction |
| 3 | PHASE-0003 | verified | PHASE-0002 | Evidence-selected supervision and recovery |
| 4 | PHASE-0004 | implementing | ARCH-0001, ROAD-0001, BENCH-0001, PHASE-0003, ADR-0019 | Operator runtime delivery and cross-domain campaign evidence |

## Benchmark contracts

| ID | Status | External authority | Product behavior measured |
| --- | --- | --- | --- |
| BENCH-0001 | accepted | Gymnasium, ARC-AGI-3, Hack The Box, and software-task evaluators | General task completion, efficiency, reliability, and recovery |

## Active experiments

| ID | Status | Question | Informs |
| --- | --- | --- | --- |
| EXP-0001 | completed | Select the initial PHASE-0003 supervisor policy (monitor window, thresholds, normalization, cooldown) from bounded seeded stagnation runs | PHASE-0003 entrance criterion |
| EXP-0002 | completed | Extend the supervisor signal coverage after the run2 false negative (evidence-repetition and budget-burn candidates per ADR-0008; run2 as the observed false-negative instance) | Revised supervisor run policy for PHASE-0004 supervised runs; Timing attempt-3 sequencing |
| EXP-0003 | completed | Burn checkpoints despite novel evidence: can the ADR-0013 P6 candidate guarantee an auditable budget checkpoint under novel-output burn at bounded exploration intervention cost | ADR-0013 P6 adoption decision; PHASE-0004 supervised-run policy |

Create an EXP document before changing an accepted algorithm on the strength
of an untested hypothesis.

## Active workstreams

| ID | Area | Current owner | Related phase |
| --- | --- | --- | --- |
| WORK-0001 | Vharness Next architecture and implementation | nutty@ba | PHASE-0004 |

## Blocked decisions

None.

## Inherited and historical material

These documents remain useful context but do not govern Vharness Next. This
classification implements the operator's explicit decision that AgentStorm
work must not impede the AVO-inspired redesign.

| Document | Classification | May govern implementation? |
| --- | --- | --- |
| `agentstorm.md` | Inherited proposal | No |
| `dynamic-safe-method-assessment.md` | Inherited phase plan | No |
| `plans.md` | Inherited roadmap | No |
| `transition.md` | Inherited transition notes | No |
| `tui-operator-console.md` | Inherited interface plan; source material for ADR-0004 | No |
| `workspace-project-foundation.md` | Inherited implementation plan | No |
| `todo` | Inherited task list | No |
| `decision-log.md` | Pre-Plant planning record | No |
| `worklog.md` | Pre-Plant planning record | No |
| `schematics/README.md` | Pre-Plant schematic instructions | No |
| `schematics/system-context.md` | Pre-Plant draft schematic | No |

## Protocol maintenance

`PROTOCOL.md`, `schema/metadata.schema.json`, and `templates/` came from the
Plant source revision named above and are ordinary repository files
(ADR-0018). Change them only in an explicit, reviewed planning change. There
is no local CLI: review each planning change against PROTOCOL §3, §5, §8, §12,
and §13 before it is merged.
