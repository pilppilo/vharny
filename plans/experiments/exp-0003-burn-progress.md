---
id: EXP-0003
title: Burn checkpoints despite novel evidence
type: experiment
status: completed
owners: [operator]
created: 2026-11-06
updated: 2026-11-08
depends_on: [PHASE-0003]
supersedes: []
related: [ADR-0008, ADR-0011, ADR-0013, ARCH-0001, EXP-0001, EXP-0002, PHASE-0004, BENCH-0001]
---

# Burn checkpoints despite novel evidence

## Question and approval boundary

Can the ADR-0013 P6 candidate guarantee an auditable budget checkpoint under
novel-output burn, while preserving truthful progress resets and bounded
intervention cost on productive exploration? This is a draft predeclaration;
operator approval freezes the plan before execution. Results do not adopt a
new shipped default. No provider or target interaction is part of this EXP.

## Baseline and controlled change

Use source `a80872c` for retained run4 evidence and the P5 baseline. Freeze the
exact experiment implementation revision and input SHA-256 manifest before
running. Compare shipped P5, EXP-0002 P4, and experiment-local P6 under the same
fixtures, seeds, budgets and scripted supervisor. Implement P6 exactly as
ADR-0013 proposes, including ledger counting and the no-supervisor-capacity
operator question. Leave the production default on P5.

ADR-0008's mandatory menu is explicit:

- Repeated actions/evidence: carry the existing action fingerprints and R1/R2
  successful stdout digests unchanged, with the EXP-0002 normalization recipe.
- Recurring failures: carry failed-receipt and nonaccepted-evaluation
  fingerprints unchanged, including budget-exhausted dispositions.
- Evaluation regression: retain EXP-0002's exclusion for this burn-definition
  comparison; run4 has no evaluation and supplies no new regression instance.
  Preserve existing regression fixtures and report their results.
- Budget burn: compare P5 first-seen-evidence resets, P4 supported-event resets,
  and P6 ledger-based supported-progress checkpoints. Evidence novelty is
  diagnostic input, never P6's reset signal.

## Inputs and procedure

1. Retain sha256-verified, read-only journal and referenced artifact copies for
   Timing runs 2 and 4. If either complete journal or required artifacts are
   unavailable, record the missing input and stop that replay; do not replace
   it with the prose report. Keep synthetic evidence explicitly labeled.
2. Reuse all six EXP-0001/0002 fixture classes unchanged: productive,
   plateaued-exploring, stalled, repetitive, regressing and recovery-drill,
   seeds 0–4. Use the existing externally scripted evaluator and supervisor.
3. Add five explicitly labeled paired classes, seeds 0–4: novel stdout every
   call without supported progress; equally novel evidence with valid supported
   progress; pending/self-reported claims only; old-objective/duplicate progress;
   and retry-heavy burn. These pairs distinguish evidence arrival from support
   without changing the old exploration labels or evaluator outcomes.
4. Run each of 55 fixtures under P5, P4 and P6, then repeat P6 for determinism:
   at most 220 offline executions. Preserve prior fixture budgets (12 main
   calls, 24 actions, 4096 context tokens, 3 evaluations, at most 4 supervisor
   calls and 2048 supervisor tokens). New classes use those same limits; add
   isolated threshold tests with a 40-call budget and no provider calls.
5. Replay both retained live journals counterfactually, recording due
   checkpoints even where historical supervision never ran. A replay cannot
   establish the outcome of guidance the historical agent never received.
6. Exercise pause, stop, steering, absent supervisor capacity, duplicate intake,
   and reopen at the threshold. Recompute the result from retained journals
   with a committed calculation recipe, without calling product monitor code
   as its own independent oracle (ADR-0010).

## Measurements and acceptance criteria

- With a 40-call budget and no supported progress, P6 must record a due
  checkpoint after 24 consumed calls, before scheduling call 25, despite novel
  stdout on every call. For run4, use ledger evidence to calculate the exact
  boundary rather than equating status counts with decision manifests.
- Valid new current-objective supported progress resets the anchor exactly
  once. Novel artifacts, pending claims, old-objective events, duplicate
  support and recovery never reset it. Retries consume budget; zero-call
  waits do not. Boundary arithmetic must agree with the independent oracle.
- Every due checkpoint has a recorded trigger, applicable recorded cooldown
  suppression, or operator escalation when capacity is insufficient. No silent
  loss; no checkpoint starts new work while paused or stopped.
- Preserve P5's historical non-fire as baseline failure evidence. Report P4
  and P6 interventions on the unchanged productive/plateaued-exploring labels
  as well as budget-checkpoint overhead; renaming the signal cannot erase that
  cost. Report trigger latency, supervisor calls/tokens, evaluator outcome,
  recovery actions, escalation frequency and any false stagnation diagnosis.
- No checkpoint may itself declare task failure, acceptance or completion.
  Supervisor usage stays within the frozen allocation; excess is a failed
  candidate, not permission to increase budget. P6 reruns must reproduce all
  trigger inputs and dispositions.
- Any missed novel-output burn checkpoint, invalid reset, replay divergence,
  silent capacity skip or changed evaluator ownership makes P6 inadmissible.
  Report all deviations; do not tune and rerun under the same declaration.
  Passing these mechanics permits a recommendation, not automatic adoption.
  The operator must explicitly accept measured exploration overhead before
  the default can change. If rejected, retain P5 and draft a new experiment.

## Artifacts, stopping rule and decision

Store the immutable manifest, copied-input hashes, fixture definitions, journals,
receipts, independent oracle recipe and aggregates under
`evidence/exp-0003-burn-progress/`. No artifacts exist yet. Stop after the
declared executions and replays, or immediately on integrity/input/authority
failure. Record failed trials without overwriting them. No unplanned provider
calls, threshold sweeps, live acceptance attempts or per-target tuning.

## Results (executed 2026-11-06)

Executed against source `07e945e` (the replay-fix commit; the fixture runs were
regenerated under it after the replay fix so the manifest digest matches the
tool). 220 offline executions (11 classes x 5 seeds x {P4, P5, P6} plus 55 P6
determinism repeats), zero censored runs, P6 reruns reproduce every trigger
input and disposition, zero pre-registration deviations, bundle
`evidence/exp-0003-burn-progress/`, `verify: OK`.

- Sensitivity (fires across 5 seeds; every pre-registered expectation met):
  P6 checkpoints on novel-stdout-no-progress 5/5, pending-claims-only 5/5,
  duplicate-progress 5/5, and retry-heavy-burn 5/5 — the novel-output burn
  that run4 exposed never passes silently. P5 stays silent on
  novel-stdout-no-progress, pending-claims-only, and retry-heavy-burn
  (the measured run4 blind spot, confirmed at fixture scale). P4 is silent
  on retry-heavy-burn (decision-counted burn cannot see retry consumption)
  and on duplicate-progress.
- Intervention cost: zero triggers on productive and on
  novel-stdout-with-progress for every policy (truthful progress keeps all
  anchors honest). P6 and P4 each fire 5 times on plateaued-exploring
  (the known exploration-intervention cost, unchanged); P5 zero.
- Acceptance-harm observations (recorded, not a rejection): on
  duplicate-progress, P4 never recovers (its duplicate-reset blindness
  leaves the run stuck for the whole budget) while P5 recovers; P6's
  checkpoint lands one call later than P5's burn (anchor 1 versus 0), and
  its four-call recovery truncates at the 12-call budget — an honest
  measured cost of the later advisory checkpoint.
- Run2 counterfactual replay (sha-pinned copy `ca9adf8f...`, 39 decisions,
  100 artifact objects verified): P6 checkpoints at boundaries 25/29/33/37
  (consumed 24/28/32/36) — exactly the claim-based P4 burn boundaries and
  the frozen pre-registration — while P5 stays silent. The shipped
  `evaluate_supervision` cross-check agrees on every trigger with the local
  P5 arm; its raw repetitions at 13-15 reflect that the shared cooldown
  lives in session bookkeeping, not in the per-boundary function. The
  fingerprint oracle reproduces 30/30 strict rows and the same 5/30
  normalized rows as the retained EXP-0002 replay (pre-existing oracle
  artifact, byte-identical mismatch list).
- Run4 counterfactual replay: **not executed** — no sha256-verified run4
  journal copy was supplied; the slice records the missing input and stops
  per this predeclaration. The run4-shaped sensitivity is covered by the
  labeled novel-stdout-no-progress and retry-heavy-burn fixture classes.
- Supervisor usage stayed within the frozen per-run allocation on every
  run; 65 due-but-cooldown-suppressed P6 checkpoints and zero operator
  escalations were recorded across the bundle.
- Measured mechanics permit a recommendation only: the operator reviews
  sensitivity and intervention cost together before any separate
  P6 adoption/default-switch decision.

Present sensitivity and intervention cost together to the operator. The
result informs a separately governed P6 adoption/default-switch decision;
transport recovery may proceed independently once ADR-0013 is accepted.

## Addendum (2026-11-08): run2 replay cross-check label drift

The frozen bundle's run2 replay cross-check calls
`evaluate_supervision(..., MonitorPolicy(), ...)` labeled "shipped": at
execution time the shipped default was the P5 composition, so the frozen
agreement with the local P5 arm reflects P5 semantics. Since the ADR-0014
switch (2026-11-06), the default `MonitorPolicy` evaluates the P6
composition, so re-running that cross-check today disagrees with the frozen
bundle by policy semantics, not by a defect in either the frozen evidence or
the current implementation. The frozen bundle is retained unchanged; a
re-run of the cross-check records `monitor.p6.v1` in its trigger payloads,
and its output is read under the policy version it records. P5 journals keep
P5 semantics and are never rewritten as P6 outcomes (ADR-0014 decision 4).
Recorded under operator decision 2026-11-08 (addendum chosen over pinning
the tool to the frozen composition).
