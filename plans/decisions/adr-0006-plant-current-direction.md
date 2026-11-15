---
id: ADR-0006
title: Use the Plant packet for current planning direction
type: decision
status: accepted
owners: []
created: 2026-10-02
updated: 2026-10-02
depends_on: []
supersedes: []
related: [PHASE-0003, EXP-0001, WORK-0001]
---

# Use the Plant packet for current planning direction

## Context

The repository instructions said, “Now: None. PHASE-0002 is `verified`; EXP-0001
awaits operator review before PHASE-0003 can become ready,” and “Do not
implement PHASE-0003 or PHASE-0004.” The Plant Required packet says, “PHASE-0003
is `ready` as of 2026-09-30 (operator-confirmed)” and “PHASE-0003
implementation may begin.” Its current handoff identifies implementation
sequence item 6 as the next action. The validator accepted the plan packet but
did not compare its prose direction with `AGENTS.md`.

The operator resolved the conflict: “Plant’s recorded readiness governs;
reconcile the stale AGENTS direction.”

## Decision

For mutable planning direction, including current phase status, readiness,
scope, blockers, and next action, use the governing documents and execution
direction supplied or referenced by the Plant Required packet, applying the
authority order in `plans/PROTOCOL.md`. The packet is a selector, not an
independent requirements source; the WORK-0001 handoff remains non-authoritative.
Repository instructions remain the source for stable engineering boundaries
and workflow constraints, but must not duplicate mutable planning status. A
conflict is resolved only when an accepted ADR explicitly resolves that same
conflict; otherwise follow the protocol and stop for operator resolution.

This records the operator's resolution of the conflict between `AGENTS.md` and
the PHASE-0003/EXP-0001 packet. It does not change any phase or experiment
status, criteria, product direction, or implementation scope. It does not
authorize PHASE-0004 work or a merge contrary to the current packet.

## Consequences

The repository startup instructions route agents to `python tools/plant.py
context plans` for current planning direction and retain only stable
restrictions. No semantic prose checker is added: the current validator checks
structured plan/index validity and selectors, not agreement between arbitrary
prose documents. Avoiding a second copy of mutable status removes this specific
failure mode without a brittle text parser.

## Evidence and references

- Operator decision in this task: Plant's recorded readiness governs; reconcile
  the stale AGENTS direction.
- `python tools/plant.py context plans` at source revision `97808d6` records
  PHASE-0003 as ready and sequence item 6 as the next action.
- `tools/plant.py`, `Checker.index` (lines 974-1025): validates indexed IDs,
  statuses, reservations, and Current focus structure; it does not compare
  direction prose with `AGENTS.md`.
