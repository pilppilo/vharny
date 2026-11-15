---
id: ADR-0007
title: Deliver the operator runtime before PHASE-0004 campaigns
type: decision
status: accepted
owners: [operator]
created: 2026-10-16
updated: 2026-10-16
depends_on: []
supersedes: []
related: [ADR-0006, ARCH-0001, ROAD-0001, PHASE-0004, BENCH-0001, SCHEM-0001]
---

# Deliver the operator runtime before PHASE-0004 campaigns

## Context

PHASE-0004 currently couples runtime implementation to frozen campaign
manifests, even though the operator has selected an expanded phase that first
delivers useful workspace and HTB execution. The repository already contains
committed runtime and evaluation work, but the phase plan does not yet govern
that work. The runtime must preserve the accepted journal, evaluation, and
external-execution boundaries while allowing real blocking I/O.

## Decision

PHASE-0004 has two sequential gates within the same phase:

1. **Runtime delivery:** provide a usable `vharness-agent` entry point for
   operator-directed workspace and HTB work through the existing domain-neutral
   kernel, memory/context policy, supervision, journal, and propose/receipt
   path. This delivery does not wait for benchmark campaign manifests.
2. **Campaign verification:** advance the four BENCH-0001 environment families
   through pinned external campaigns. All four remain required before
   PHASE-0004 can be verified.

The session coordinator is the sole writer of session journal, projections,
and session-state transitions. Bounded workers may perform blocking execution,
model, or supervisor calls and return serialized or typed results. They have no
independent scheduling or dispatch authority and cannot mutate session state.
The runtime worker owns its separate runtime execution store; this does not
change session-journal ownership. CLI control clients submit over local IPC to
the coordinator transport inbox; in-process workers return results through the
same bounded inbox.

Where no external check command is configured, evaluation mode is an explicit,
typed, persisted run setting selecting operator evaluation. The application
must not infer this mode from prose such as an acceptance-semantics string.
Operator decisions and checker results may be admitted and durably retained
while paused or stopped. They are consumed or promoted only while the session
is schedulable and the request, objective, attempt, and lineage head remain
applicable. A stopped session is terminal; retained intake is evidence only.

Recovery preserves uncertain outcomes. Missing receipts and dead coordinators
do not prove that an effect did not occur. Do not automatically resend an
uncertain operation, kill a recovered PID without verified ownership, or claim
that stopping or killing reverses effects. Runtime worker loss may lose live
handles while subprocesses, descendants, and external effects survive.

No merge into `next` is planned for the foreseeable future, and no merge is
part of this phase. The legacy default entry point remains in place for this
milestone. Any later merge and any default-entrypoint switch each require a
separate future operator decision.

## Consequences

The runtime gate can make Vharness useful for real operator tasks while the
phase remains `implementing`. It is not a phase-completion or broad-capability
claim. The gate requires external acceptance of each predeclared objective or
milestone in one unscripted codebase task and one operator-selected HTB task;
failed trials remain visible as failures. The HTB objective and acceptance
condition come from the operator, and a scan cannot stand in for an access or
box-completion request. Campaign manifests and results remain necessary for
full PHASE-0004 verification.

This decision supplements ADR-0006 and does not supersede or amend its accepted
text. It adds no authorization-verification service, credential store, target
lifecycle control, policy gate, side-effect audit system, or duplicate grader.

## Related implementation map

`specs/proto/06-workspace-runtime.md` records the detailed runtime and session
contract changes. It is subordinate to this ADR and ARCH-0001.
