---
id: ADR-0019
title: PHASE-0004 gates are independent; ARC-AGI-3 work proceeds before the acceptance runs
type: decision
status: accepted
owners: [operator]
created: 2026-11-16
updated: 2026-11-16
depends_on: [ADR-0002, ADR-0003, ADR-0005]
supersedes: [ADR-0007]
related: [ADR-0009, ADR-0018, ARCH-0001, ROAD-0001, PHASE-0004, BENCH-0001, SCHEM-0001, WORK-0001]
---

# PHASE-0004 gates are independent; ARC-AGI-3 work proceeds before the acceptance runs

## Context

ADR-0007 gave PHASE-0004 "two sequential gates within the same phase":
runtime delivery, then campaign verification. The runtime delivery gate
requires external acceptance of one unscripted codebase task (Omarchy Task 1)
and one operator-selected HTB task (Timing Task 2, under ADR-0009).

Neither acceptance has been obtained. Timing Task 2 has thirteen retained
failures (`run1`–`run13`) and the operator dropped run14 on 2026-11-16.
Omarchy Task 1 has no recorded run outcome. The materials for both tasks are
not in this repository copy, and no run is staged. Under the sequential order,
all campaign work would wait on them.

The repository has no ARC-AGI-3 integration, although ARC-AGI-3 is one of the
four campaign families the phase requires.

The operator decided on 2026-11-16: "omarchy and htb will not be the next
runs. we will move forward with arc-agi-3 adapters". Asked whether scored
ARC-AGI-3 campaign runs may happen before the acceptance tasks pass, the
operator answered yes: "ARC-AGI-3 can go all the way to scored results."

## Decision

PHASE-0004 keeps two gates, and both remain required before the phase can be
verified:

1. **Runtime delivery:** a usable `vharness-agent` entry point for
   operator-directed workspace and HTB work through the existing
   domain-neutral kernel, memory/context policy, supervision, journal, and
   propose/receipt path, with the external acceptance evidence PHASE-0004
   names.
2. **Campaign verification:** the four BENCH-0001 environment families
   advanced through pinned external campaigns.

The gates are no longer sequential. Campaign work for a family, including its
environment integration, manifest freeze, smoke tests, baseline, and scored
runs, may proceed before the runtime delivery gate has passed. A scored run
still requires its frozen BENCH-0001 campaign manifest before any target
result is seen.

The Timing Task 2 and Omarchy Task 1 acceptance runs are deferred. They are
not dropped: the runtime delivery gate still requires them, ADR-0009 still
names the HTB acceptance target, and retained failures stay failures. Resuming
either needs the operator's word and fresh staging against the current
`main`.

The current work is the ARC-AGI-3 integration, followed by its campaign.

The remaining decisions of ADR-0007 continue in force and are restated here
so that they keep an accepted source:

- The session coordinator is the sole writer of the session journal,
  projections, and session-state transitions. Bounded workers may perform
  blocking execution, model, or supervisor calls and return serialized or
  typed results. They have no independent scheduling or dispatch authority and
  cannot mutate session state. The runtime worker owns its separate runtime
  execution store; this does not change session-journal ownership. CLI control
  clients submit over local IPC to the coordinator transport inbox; in-process
  workers return results through the same bounded inbox.
- Where no external check command is configured, evaluation mode is an
  explicit, typed, persisted run setting selecting operator evaluation. The
  application must not infer this mode from prose such as an
  acceptance-semantics string. Operator decisions and checker results may be
  admitted and durably retained while paused or stopped. They are consumed or
  promoted only while the session is schedulable and the request, objective,
  attempt, and lineage head remain applicable. A stopped session is terminal;
  retained intake is evidence only.
- Recovery preserves uncertain outcomes. Missing receipts and dead
  coordinators do not prove that an effect did not occur. Do not automatically
  resend an uncertain operation, kill a recovered PID without verified
  ownership, or claim that stopping or killing reverses effects. Runtime
  worker loss may lose live handles while subprocesses, descendants, and
  external effects survive.
- The runtime gate requires external acceptance of each predeclared objective
  or milestone in one unscripted codebase task and one operator-selected HTB
  task; failed trials remain visible as failures. The HTB objective and
  acceptance condition come from the operator, and a scan cannot stand in for
  an access or box-completion request.
- No merge into `next` is planned for the foreseeable future, and no merge is
  part of this phase. The legacy default entry point remains in place. Any
  later merge and any default-entrypoint switch each require a separate future
  operator decision.

This decision adds no authorization-verification service, credential store,
target lifecycle control, policy gate, side-effect audit system, or duplicate
grader.

## Consequences

The ARC-AGI-3 integration is built as an environment adapter under ADR-0003:
serialization and protocol mechanics only, with no ARC-specific prompts or
planning. Under ADR-0002 the official ARC-AGI-3 scorecard is the only
authority for completion and score; the adapter does not grade.

A campaign rung can be reported as achieved while the runtime delivery gate is
open. PHASE-0004 stays `implementing` until its full scoped work exists, and
is `verified` only when both gates and every exit criterion have linked
evidence. A published result must state which gate and which rungs it covers.

If the acceptance tasks stay deferred, PHASE-0004 cannot be verified. That is
the accepted cost of this ordering, and it remains visible in the index.

`specs/proto/06-workspace-runtime.md` remains the detailed runtime and session
contract map. It is subordinate to this ADR and ARCH-0001.

## Alternatives considered

- Keep the sequential order and build only the ARC-AGI-3 adapter and smoke
  test, holding scored runs until the acceptance tasks pass. Rejected by the
  operator.
- Drop the Timing and Omarchy acceptance tasks. Not chosen: the operator
  deferred them and did not change the runtime gate's evidence requirement.
- Amend ADR-0007 in place. Not permitted: accepted ADR text is immutable
  except for corrections that do not change its meaning (PROTOCOL §6).

## Evidence and references

- Operator decisions, 2026-11-16, quoted above.
- ADR-0007, superseded by this decision.
- WORK-0001 Current handoff: Timing `run1`–`run13` retained failures, run14
  dropped, Omarchy Task 1 without a recorded outcome.
- `src/vharness/agent/adapters/` at `9951274` contains the Gymnasium and
  workspace adapters and no ARC-AGI-3 adapter.
- Linear COD-32 (this planning change) and COD-33 (ARC-AGI-3 adapter).
