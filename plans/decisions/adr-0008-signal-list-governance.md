---
id: ADR-0008
title: Supervisor negative-signal families are a mandatory candidate menu
type: decision
status: accepted
owners: [operator]
created: 2026-10-29
updated: 2026-10-29
depends_on: []
supersedes: []
related: [ARCH-0001, PHASE-0003, EXP-0001, PHASE-0004, BENCH-0001]
---

# Supervisor negative-signal families are a mandatory candidate menu

## Context

ARCH-0001's supervision section names four negative-signal families — repeated
normalized actions or evidence, recurring failure fingerprints, evaluation
regression, and budget burn without supported outcome or knowledge progress —
while delegating only "window, thresholds, normalization, and cooldown" to
experiment selection. No accepted document stated whether the four families
themselves are selectable, and the list was narrowed in sequence without a
recorded decision: PHASE-0003's entrance gate tests repetition only; EXP-0001's
selected policy `monitor.p1.v1` fires on "one normalized action or evidence
fingerprint" while its normalization specification covers action arguments
only, leaving the evidence half undefined; and the shipped monitor
(`supervision.py::_event_facts`) fingerprints action arguments, failed
receipts, and non-accepted evaluation dispositions — no evidence content and
no burn trigger.

Run 2 of the PHASE-0004 Timing acceptance task (2026-10-28) is the first live
instance of the gap: the shipped monitor produced zero triggers across all 39
decision boundaries while the model consumed the full budget without an
exploitation attempt, and run 2 recorded zero supported progress or knowledge
events. Committed counterfactual analysis shows an evidence-content fingerprint
would have fired at boundaries 12 and 17 on the repeated empty-output digest,
and a budget-burn-without-progress trigger would have fired from call 24 at a
60% threshold
(`coordination/diagnostics/acceptance-20261028/timing-accept-attempt-2-review.md`
and `review-data/`).

## Decision

1. The four negative-signal families are a **mandatory candidate menu**. Every
   monitor or supervision experiment must evaluate a candidate policy for each
   family — repeated normalized actions or evidence, recurring failure
   fingerprints, evaluation regression, and budget burn without supported
   outcome or knowledge progress — and either carry it into the selected
   policy or record an exclusion rationale. Silent omission of a family is a
   planning defect, not a scoping choice.
2. An **evidence fingerprint** is a normalized digest of the content of
   evidence the agent received (tool output, receipt payload, or observation),
   computed independently of the action that produced it. Every experiment
   must state which received artifacts it fingerprints and how.
3. ARCH-0001 carries this definition and the menu obligation as a
   meaning-preserving clarification. Its accepted signal list and its
   parameters-only delegation to experiments are otherwise unchanged.
4. PHASE-0003's `verified` status stands: its entrance gate tested the family
   it scoped, under the text as written. The status records compliance with
   its predeclared seeded exit criteria, not a live demonstration that
   stagnation is caught in real runs; BENCH-0001's live campaigns are where
   live benefit and false-intervention cost are measured.

## Consequences

- The next supervisor experiment must include evidence-repetition and
  budget-burn candidates — run 2 is the failure evidence ROAD-0001's "add
  algorithmic complexity only after benchmark or failure evidence" principle
  requires — and must carry an evaluation-regression candidate or record its
  exclusion rationale.
- EXP-0001's recorded P2 exclusion (false positives on productive runs; the
  burn trigger never fired earlier than the repetition trigger under those
  budgets) is the template for future exclusions; the evidence-repetition
  family had no such record and now requires one.
- The shipped monitor remains valid until a later experiment selects a revised
  policy; run 2 stands as retained evidence of the coverage gap.
