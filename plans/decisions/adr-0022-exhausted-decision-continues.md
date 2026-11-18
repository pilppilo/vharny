---
id: ADR-0022
title: A decision that never answers costs its calls, not the run
type: decision
status: accepted
owners: [operator]
created: 2026-11-18
updated: 2026-11-18
depends_on: [ADR-0013, ADR-0015, ADR-0017, ADR-0021]
supersedes: []
related: [ARCH-0001, PHASE-0004, BENCH-0001]
---

# A decision that never answers costs its calls, not the run

## Context and decision requested

ADR-0017 §4 makes a decision terminal when it is still empty or cut off after
its re-ask: the run ends with `output-exhausted`. ADR-0015 §4 left that class
out of its operator-resumable wait for the same reason.

The LongCat-2.5 trial (`evidence/phase-0004-longcat-trial/`) showed the cost:

- `ft09` ended after 24 minutes with zero game actions. Its first decision ran
  out at every answer limit up to 32,768 tokens and on the re-ask.
- The `ls20` trial run ended the same way, with most of its action budget left.

The same trial showed that LongCat's reasoning length varies a lot from one try
of a decision to the next. One decision ran out at 16,384 tokens, then answered
at the next try in 1,591. So one decision that never answered says little about
the next one. Ending the game over it throws away the rest of the game's budget
on a stochastic event.

The operator approved this change on 2026-11-18. In answer to a review of the
re-run, they asked for the issue to be filed and started, and for the plans to
be updated (Linear COD-92).

## Decision

### 1. The session decides again

When a decision ends `output-exhausted`, the coordinator records it as before
(`model_malformed` with all its attempts, repair notes and spent calls). It
does not end the run, enter a wait, or retry the same request. The session
takes its next decision on the next tick.

### 2. The model sees that its last decision failed

A failed decision appears in the model's recent history with its failure class,
for example `failure_class=output-exhausted`. The next request therefore
differs from the failed one, and the model is told its last attempt produced no
answer.

### 3. Bounds

Three decisions in a row that end `output-exhausted` end the run, as ADR-0017
§4 did for one. The error names the count. The count:

- is derived from the journal, so it survives a restart;
- restarts at any decision that answers;
- ignores supervisor calls, as the transport-recovery window does.

The model-call budget, and the step and other limits, bound everything else.

### 4. What stays unchanged

These are unchanged:

- ADR-0017's and ADR-0021's re-asks within one decision;
- the transport retry policy and ADR-0013's provider-recovery wait;
- ADR-0015's wait for malformed output.

`Session.step`, the synchronous path used by scripted drivers, still raises on
an exhausted decision; the new policy lives in the coordinator, as the recovery
policy does.

## Why a cap of three rather than the budget alone

The budget alone would let a model that never answers spend a whole game's
calls. That is about 24 minutes a decision with LongCat, and it is a cost,
not a result. Across the trial and the re-run, 2 of 24 decisions ended
`output-exhausted`, and each ended its run. If those odds held independently,
three in a row would be well under one in a thousand. So three in a row means
the run has stopped answering. The cap is a constant, not a setting, so
campaign arms cannot differ on it.

## Consequences

- A campaign game no longer ends at its first decision that never answers.
  Results before and after this change differ in how runs end and must not be
  pooled.
- A run can now record `output-exhausted` decisions in the middle of its
  journal. Replay already accepts a failed decision followed by more
  decisions, as the transport-retry path produces.
- COD-86 adds the named "budget exhausted" outcome. Until then, a budget that
  runs out after such decisions ends the run with the existing budget error.

## Dependents updated in the same change

- ARCH-0001 'Failure and recovery behavior': the ADR-0017 paragraph.
- ADR-0017 §4 gains a note pointing here.
- The index gains the ADR-0022 row.
- WORK-0001 records the decision.

## Alternatives considered

- **Keep ending the run (ADR-0017 §4).** Rejected on the trial evidence.
- **Enter the operator-resumable wait, as ADR-0015 does for malformed
  output.** Rejected for campaign runs, which are unattended: a wait stalls the
  game until a person answers.
- **Retry the identical request.** Rejected: it repeats the same odds and
  hides the failure from the model.
