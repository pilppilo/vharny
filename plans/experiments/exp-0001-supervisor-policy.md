---
id: EXP-0001
title: Select the initial PHASE-0003 supervisor policy
type: experiment
status: completed
owners: []
created: 2026-09-27
updated: 2026-09-27
depends_on: [PHASE-0002]
supersedes: []
related: [ARCH-0001, PHASE-0003, BENCH-0001]
---

# Select the initial PHASE-0003 supervisor policy

## Falsifiable question

Which versioned monitor policy — window, thresholds, normalization, and
cooldown — best separates productive plateaus from stagnation in bounded seeded
runs? A policy beats baseline if advisory intervention under it improves
externally accepted results more than operator-only intervention, within a
predeclared false-intervention cap and budget.

## Decision this experiment informs

The PHASE-0003 entrance criterion that requires a linked EXP to select the
initial supervisor policy. ARCH-0001 (§ "Progress, supervision, and variation")
fixes the monitor's signals and escalation path but deliberately chooses no
numerical trigger defaults; they are versioned run policy chosen from a
PHASE-0003 experiment. PHASE-0003 cannot become `ready` until this EXP is
`completed` and selects that policy (PROTOCOL §8).

## Baseline

The verified PHASE-0002 kernel without a monitor: stagnation is observable
only through operator steering or external evaluation; no automatic trigger
exists. Baseline runs (P0) use the same seeded stagnation fixtures and budgets
as candidate runs, with intervention available only via the operator path.

## Controlled change

Candidate monitor policies, each a versioned run policy over the unchanged
kernel, with initial parameterizations fixed before `running`:

- P1 `monitor.p1.v1` repetition trigger: rolling window of 8 main model calls;
  fire when one normalized action or evidence fingerprint repeats 3 times
  within the window without supported outcome or knowledge progress;
  normalization is lowercase, whitespace collapse, and canonical JSON of
  action arguments; cooldown 4 calls.
- P2 `monitor.p2.v1` budget-burn trigger: fire when 60% of the action budget
  (15 of 24) or 60% of the token budget (2458 of 4096) is consumed without
  supported outcome or knowledge progress; cooldown 4 calls.
- P3 `monitor.p3.v1` combined: P1 ∪ P2; after any trigger, re-arm only on a
  supported progress event or after a 6-call cooldown.

All other kernel behavior, fixtures, seeds, budgets, and normalization rules
are held constant across P0–P3. Later material algorithm changes require a
later EXP justified by observed false positives, false negatives, or benchmark
cost (PHASE-0003).

## Run plan (operator-approved 2026-09-27)

Fixtures and sample:

- Six labeled scripted fixture classes: `productive` (progress control),
  `plateaued-exploring` (productive plateau, false-positive trap), `stalled`,
  `repetitive`, `regressing`, and `recovery-drill` (stagnation with a viable
  way out).
- Seeds 0–4 per class. Policies P0–P3 paired by class and seed:
  6 × 5 × 4 = 120 scripted runs.
- The P0 condition repeats once per fixture (30 re-executions) as a
  determinism check; total ≤ 150 executions.
- Fixtures are deterministic scripted models and scripted environments plus a
  fixture-owned external evaluator; no provider calls and no network.

Budgets per run:

- Main model calls ≤ 12, external actions ≤ 24, context ≤ 4096 tokens,
  evaluation requests ≤ 3.
- P0: no supervisor calls. P1–P3: supervisor allocation ≤ 4 calls and
  ≤ 2048 tokens per run — the only permitted budget delta.
- Total: ≤ 1800 main-call and ≤ 360 supervisor-call slots
  (≤ 2160 scripted calls).

Scaffolding:

- Candidate monitor implementations and fixtures live in experiment-local
  scaffolding over public kernel/journal APIs; they do not change the kernel
  or PHASE-0003 modules.
- The run manifest freezes fixture definitions, policy versions, seeds,
  budgets, and the exact revision used.

## Measurements

- External evaluator result after intervention versus before; the external
  fixture evaluator owns scoring.
- False-intervention rate: triggers on `productive` and
  `plateaued-exploring` runs.
- Trigger latency: main model calls from stagnation onset to supervisor
  notice.
- Cost: supervisor calls, tokens, and wall time per run.
- Time to recovery: external actions from stagnation onset to the first
  externally accepted recovery.
- Every trigger records its inputs (fingerprint, window contents, budget
  counters, onset cursor) so false intervention and cost are measurable
  (ARCH-0001).

## Procedure

1. Build the experiment-local scaffolding: scripted fixtures, monitor
   implementations P1–P3, the runner, and the aggregate script.
2. Freeze the run manifest (fixtures, policy versions, seeds, budgets,
   revision) before execution.
3. Run P0 (twice per fixture) and P1–P3 (once per fixture and seed) over the
   fixed budgets; retain journals, evaluator receipts, and trigger-input logs.
4. Reproduce the aggregate table from the retained journals with the bundle's
   calculation script.
5. Compare measurements against the predeclared tolerances below and record
   the result.

## Tolerances (operator-approved 2026-09-27)

Any revision requires a new recorded operator approval before `running`.

- A candidate is accepted only if (a) external acceptance strictly improves
  over its paired P0 run in at least 3 of the 20 stagnation-pair comparisons
  (`stalled`, `repetitive`, `regressing`, `recovery-drill` × 5 seeds) and
  (b) actions from onset to first externally accepted recovery improve over
  P0 in at least 2 of those 4 classes — a class where P0 never recovers but
  the candidate does counts as improvement — with at least 3 of 5 seeds
  improving in each counted class.
- A candidate is rejected if any trigger fires on the 5 `productive` control
  runs; or more than one trigger fires per `plateaued-exploring` run; or
  supervisor calls or tokens exceed the allocation in any run; or any
  duplicate external effect or replay divergence appears.
- The result is inconclusive, not a pass, if any class has fewer than 5
  completed seeds, more than one censored or budget-exhausted run, or the P0
  condition does not reproduce across its two executions.
- If multiple candidates pass, select the fewest false triggers, then lowest
  supervisor cost, then lowest median trigger latency; ties by policy ID.

## Stopping rule

The EXP ends when P1–P3 are each measured against P0, or when the fixed run
budget (150 executions; ≤ 2160 scripted calls) is exhausted. If no candidate
meets the tolerances, the result is "no policy selected"; PHASE-0003 stays
`accepted` with the question reopened.

## Artifact locations

- Seeded fixture journals, evaluator receipts, trigger-input logs, the run
  manifest, the runner, and the aggregate calculation script:
  `evidence/phase-0003-supervisor-policy/`.
- This document's Results section records the final measurements and the
  selected policy or the rejection.

## Results

Executed 2026-09-27 from revision `aaaa7458c0064f69990c13b787cb9eb21773df77`
(tool `tools/exp_0001_supervisor_policy.py`; zero provider calls). The retained
bundle is `evidence/phase-0003-supervisor-policy/`: 150 scripted executions
(six fixture classes × seeds 0–4 × P0–P3, plus 30 P0 determinism
re-executions), per-run canonical event streams, trigger-input logs,
`manifest.json`, `results.json`, and `report.md`.

| Candidate | Improved stagnation pairs | Classes ≥3/5 seeds | False triggers (productive) | False triggers (exploring) | Supervisor calls | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | 20/20 | 4/4 | 0 | 0 | 20 | selected |
| P2 | 20/20 | 4/4 | 0 | 5 | 25 | not selected |
| P3 | 20/20 | 4/4 | 0 | 5 | 25 | not selected |

All three candidates met the predeclared acceptance tolerance; the P0
baseline never recovered in the four stagnation classes and reproduced
exactly across its repeat executions. P2 and P3 each fired once per
`plateaued-exploring` run — a false intervention that did not change
acceptance — and P2's budget-burn trigger never fired earlier than the
repetition trigger under these budgets. The predeclared selection order
(fewest false triggers, then lowest supervisor cost, then lowest median
trigger latency) selects **P1** (`monitor.p1.v1`: window 8, repetition
threshold 3, cooldown 4) as the initial PHASE-0003 supervisor policy.

## Interpretation and limitations

The selected P1 policy is the initial PHASE-0003 default for its versioned
window, threshold, normalization, and cooldown; P2 and P3 remain unselected
candidates. The seeded scripted sample bounds the claim: it measures trigger
separation on labeled fixtures, not live-task benefit, and BENCH-0001 later
measures false intervention and cost in live runs. Later EXPs may revise
thresholds when real failure traces or benchmark cost justify it (PHASE-0003).
