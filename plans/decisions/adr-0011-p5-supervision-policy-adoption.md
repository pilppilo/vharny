---
id: ADR-0011
title: P5 supervision policy adoption
type: decision
status: superseded
owners: [operator]
created: 2026-11-01
updated: 2026-11-06
depends_on: []
supersedes: []
related: [ADR-0008, PHASE-0004, EXP-0002, BENCH-0001]
---

# P5 supervision policy adoption

## Context

EXP-0002 (completed 2026-10-30) executed its predeclared selection over
candidate supervision policies evaluated against the ADR-0008 mandatory
signal menu and selected P5: "The predeclared selection order (fewest false
triggers, then lowest supervisor cost, then lowest median trigger latency,
ties by policy ID) selects **P5 `monitor.p5.v1`**." EXP-0002 and WORK-0001
record that adoption is a separate operator decision and a kernel change
outside the experiment's scope; the shipped default remains
`monitor.p1.v1`.

On 2026-10-31 the operator was presented the selection with its recorded
trade:

- P5's burn trigger is deliberately blind to the run2 failure shape (churn
  that looks like progress); run2's coverage comes from the evidence-repeat
  fingerprints both leading candidates share.
- P5 produces zero false interventions on plateaued-exploring runs where
  the alternative P4 falsely intervened in every run.
- The evaluation base is seeded scripted fixtures (six classes × five
  seeds) plus one live replay (n=1), and BENCH-0001 campaigns are where
  live benefit and false-intervention cost are measured.

## Decision

1. The operator adopts `monitor.p5.v1` as the shipped default supervision
   policy, superseding `monitor.p1.v1`.
2. This decision is recorded, not implemented. The kernel change that
   switches the shipped default is explicitly deferred: the shipped
   implementation continues running `monitor.p1.v1` until a separately
   gated, committed change implements the switch. Repository fixes are in
   progress and no acceptance run (Timing attempt-3) is scheduled before
   that implementation, so no run falls under this decision until the
   switch lands.
3. Until the implementation change lands, the shipped default remains
   `monitor.p1.v1`; nothing here amends phase text, EXP-0002's record, or
   any status.

## Consequences

- A later, separately gated change implements the switch and is named in
  WORK-0001's next action; it carries the usual gates (needs-test, full
  verification under `baton:verify`).
- The adopted trade is accepted on record: weaker burn-shape coverage for
  run2-like failures (covered via evidence-repeat fingerprints) in exchange
  for zero false interventions on plateaued-exploring runs, versus P4's
  every-run false interventions.
- Live benefit and false-intervention cost remain to be measured by the
  frozen BENCH-0001 campaigns; a successor decision may supersede this one
  on that evidence.
