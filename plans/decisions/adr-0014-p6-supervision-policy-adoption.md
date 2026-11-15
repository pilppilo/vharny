---
id: ADR-0014
title: P6 supervision policy adoption
type: decision
status: accepted
owners: [operator]
created: 2026-11-06
updated: 2026-11-06
depends_on: [ADR-0013]
supersedes: [ADR-0011]
related: [ADR-0008, ARCH-0001, EXP-0003, PHASE-0004, BENCH-0001]
---

# P6 supervision policy adoption

## Context

EXP-0003 (completed 2026-11-06, `evidence/exp-0003-burn-progress/`) measured
the ADR-0013 P6 candidate against shipped P5 and comparator P4 across 220
offline executions plus the sha-pinned run2 counterfactual replay:
admissible, zero pre-registration deviations, P6 determinism reproducible.
P6 checkpointed every burn shape including novel-output burn (the retained
run4 failure mode) and never interrupted productive or truthful-progress
fixtures; P5 stayed silent on the novel-output shapes. On run2, P6
checkpointed exactly at the frozen boundaries (consumed 24/28/32/36) while
P5 stayed silent.

On 2026-11-06 the operator decided:

> Adopt P6 as the shipped monitor. Record the 5/5 quiet-exploration
> interventions and the late-recovery case as known costs; verify the
> switch through the governed process. Keep the retained failures
> unchanged. No live run is authorized by this decision.

## Decision

1. `monitor.p6.v1` replaces `monitor.p5.v1` as the shipped supervision
   default, implemented as a separately gated kernel change (the ADR-0011
   precedent). ADR-0011's accepted text is immutable and remains the
   shipped default until that change lands; the same change flips
   ADR-0011 to `superseded` in the index.
2. Known costs are accepted and recorded, not defects: P6 intervened in
   all five quiet-exploration fixtures (the same exploration-intervention
   cost previously accepted against P4's counter-shape), and on the
   duplicate-progress fixture its later checkpoint left too little budget
   for the four-call recovery to finish. Five simulated runs per class are
   a small sample; future supervised runs and the BENCH-0001 campaigns
   must keep measuring both costs against this record.
3. Retained failures are unchanged: Timing attempts 1-4 stand as recorded,
   the section 8 stop rule still binds, and this decision authorizes no
   live run, no attempt 5, and no merge into `next`.
4. No shipped-monitor semantics change for historical journals: P5
   journals keep P5 semantics and are never rewritten as P6 outcomes, per
   ADR-0013's journaling rules.

## Implementation and acceptance boundary

The switch change implements the P6 semantics in `supervision.py` (ledger-
based supported-progress anchor, shared cooldown with repetition
precedence, the `budget-checkpoint` cause with consumed/threshold/anchor
journaling, and replay acceptance for the new cause), with the supported-
progress projection in `memory.py` where ADR-0013 assigns it, updates
ARCH-0001's Failure-and-recovery and Progress-supervision sections,
SCHEM-0001's affected paths, and the affected proto contracts in the same
governed change (ADR-0013's Dependents and adoption sequence), and runs the
repository's full verification once under `baton:verify` against the
committed switch. The experiment-local reference implementation lives in
`tools/exp_0003_burn_progress.py` and stays experiment-local; retained
evidence tools keep their own pinned policy versions.

## Alternatives and limits

Retaining P5 preserves zero quiet-exploration interventions but leaves the
measured novel-output blind spot that motivated this sequence. P6 trades
that blind spot for accepted exploration and latency costs whose live
magnitude is unknown until the campaigns run; this decision authorizes no
live measurement.
