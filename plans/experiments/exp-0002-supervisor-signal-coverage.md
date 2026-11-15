---
id: EXP-0002
title: Extend the supervisor signal coverage after the run2 false negative
type: experiment
status: completed
owners: [faggyjoe@expinsider]
created: 2026-10-30
updated: 2026-10-30
depends_on: [PHASE-0003]
supersedes: []
related: [ARCH-0001, ADR-0008, EXP-0001, PHASE-0004, BENCH-0001]
---

# Extend the supervisor signal coverage after the run2 false negative

## Falsifiable question

Does extending the selected monitor policy with evidence-content repetition
fingerprints and a budget-burn-without-progress trigger convert live
stagnation of the kind journaled in the 2026-10-28 Timing acceptance attempt 2
("run2": 39 main model calls of reconnaissance, zero exploitation attempts,
zero supported progress events, 41 distinct action fingerprints, no trigger)
into timely supervisor intervention, while staying within the EXP-0001
false-intervention tolerances on the seeded fixture suite? A candidate is
admissible only if it fires on the run2 journal replay within its
pre-registered boundaries and passes the fixture tolerances; the shipped
policy is replaced only if a candidate is selected.

## Decision this experiment informs

- ADR-0008 decision item 1: the four negative-signal families are a mandatory
  candidate menu; the next supervisor experiment must include
  evidence-repetition and budget-burn candidates, with run2 as the failure
  evidence, and a candidate or a justified exclusion for evaluation
  regression.
- PHASE-0003 implementation sequence step 7 authorizes material monitor
  algorithm changes through a later EXP justified by observed false positives
  or false negatives; run2 is the journaled false-negative instance
  (`coordination/diagnostics/acceptance-20261028/`, committed at `c4d7ab0`).
- The result selects whether subsequent supervised runs (remaining PHASE-0004
  acceptance attempts and the BENCH-0001 campaigns) run on a revised versioned
  policy or keep `monitor.p1.v1` (ADR-0008 consequence: the shipped monitor
  remains valid until a later experiment selects a revised policy). It also
  informs the Timing attempt-3 sequencing decision, which stays with the
  operator.

## Baseline

The shipped `monitor.p1.v1` (window 8, repeat threshold 3, cooldown 4), whose
implemented fingerprints (`supervision.py::_event_facts`) cover action
arguments, failed receipts, and non-accepted evaluation dispositions,
including the synthetic `budget exhausted` evaluation fingerprint for
budget-rejected dispatches; there is no evidence-content fingerprint and no
burn trigger (ADR-0008 Context). The attempt-2 record's §1.1 names only the
first two of these; ADR-0008 and the shipped code are authoritative.
Computed replay over the run2 journal with the shipped implementation:
zero triggers at all 39 decision boundaries; the maximum any fingerprint
reaches inside any window is 1 against the threshold of 3; 41 distinct
fingerprints, none repeating
(`coordination/diagnostics/acceptance-20261028/timing-accept-attempt-2-review.md`
§2). The EXP-0001 seeded fixture suite and its paired-measurement method are
the second baseline component: the false-intervention bar is unchanged.

## Controlled change

Candidate monitor policies, each a versioned run policy over the unchanged
kernel, with parameterizations fixed before `running`. Per ADR-0008 item 1,
each of the four negative-signal families carries a candidate or a recorded
exclusion. Per ADR-0008 item 2, the fingerprinted evidence artifacts are
stated explicitly.

Family 1 — repeated normalized actions or evidence:

- The action-argument fingerprint is carried forward unchanged from
  `monitor.p1.v1` (lowercase, whitespace collapse, canonical JSON of action
  arguments).
- R1 byte-level evidence fingerprint: for every successful `exec` receipt,
  SHA-256 of the stdout payload exactly as received — the review's computed
  `stdout_strict` variant. Stderr is not included: run2's repeated credential
  loop suppressed stderr, and the computed counterfactual hashed stdout only;
  adding stderr would be an uncomputed extension requiring fresh sizing.
  Pre-registered run2 expectation: fires at boundaries 12 and 17 on the
  repeated empty stdout digest (attempt-2 review §3 corrected;
  `review-data/counterfactual_a_fires.csv`).
- R2 normalized evidence fingerprint: the same stdout payloads after
  stripping echo markers and transport headers, lowercasing, and whitespace
  collapse — the review's computed `stdout_normalized` variant.
  Pre-registered run2 expectation: boundaries 12 and 17. Known caveat carried
  from review §3: marker-only chunks normalize to the empty digest and can
  read as false empty repeats; the fixture suite measures this.
- Artifact-read content digests and receipt-envelope digests were computed
  against run2 in the attempt-2 review (0 triggers) and are recorded here as
  computed-non-candidates for this failure shape; they are not carried.
  Semantic evidence extraction (normalized facts such as listener sets or
  HTTP result classes) is out of scope for this EXP per operator direction
  (2026-10-30); R2 is the scoped approximation, and a later EXP may add
  semantic fingerprints if the fixture suite exposes blind spots.

Family 2 — recurring failure fingerprints: carried unchanged. The
`monitor.p1.v1` failure fingerprints remain in every candidate: the
failed-receipt fingerprint and the non-accepted evaluation-disposition
fingerprint, including the synthetic `budget exhausted` fingerprint for
budget-rejected dispatches. Run2 recorded zero failed receipts and one
non-accepted evaluation (its final honest reject), so this family was
correctly silent there; no observed false negative justifies changing it.

Family 3 — evaluation regression: excluded with rationale. No
evaluation-regression instance exists in any retained live run (attempt 1
ended in a provider transport failure before evaluation; attempt 2 produced
one honest typed operator reject with nothing to accept). A candidate could
be sized only by fixtures, with no live instance to pre-register it against,
and would add trigger surface ahead of evidence. Recorded per ADR-0008
item 1; revisit in a later EXP when a live instance exists.

Family 4 — budget burn without supported outcome or knowledge progress:

- B1 claim-based burn trigger: fire when 60% of the main model-call budget is
  consumed without a supported progress or knowledge event; cooldown 4 calls.
  The progress counter keeps the EXP-0001 definition (supported
  `progress_claim_recorded` / `knowledge_recorded` events only), deliberately
  retaining the bookkeeping dependency identified in run2 finding 1.2.
  Pre-registered run2 expectation: fires at calls 24, 28, 32, 36 (attempt-2
  review §4, 60% row). Action-budget and token-budget burn variants are not
  carried: the computed sizing covers the call budget only.
- B2 claim-free burn trigger: same 60% threshold and cooldown, but the
  progress counter is derived from the journal without model bookkeeping: a
  call is progress-bearing when the agent received, since the last
  progress-bearing call, at least one successful receipt whose R1 or R2
  fingerprint had not been seen before in the attempt. Pre-registered run2
  expectation: it does NOT fire on the non-empty portion of run2 — the
  review's seen-artifacts map shows non-empty outputs were byte-distinct on
  every repeat (echo labels, header dates, section ordering), so first-seen
  evidence keeps arriving and B2 counts churn as progress. This blindness is
  the pre-registered cost of removing the bookkeeping dependency; the fixture
  suite measures whether B2 trades it for fewer false interventions than B1.
  The non-fire expectation is an inference from the review's seen-artifacts
  map — the review computed only the claim-based burn counterfactual — so the
  run2 replay measures it.

Combined candidates (they differ only in the burn trigger's progress
definition):

- P4 `monitor.p4.v1`: action fingerprint ∪ failed-receipt fingerprint ∪
  non-accepted evaluation-disposition fingerprint (every shipped signal,
  carried unchanged) ∪ R1 ∪ R2 ∪ B1; after any trigger, re-arm after the
  4-call cooldown; the implemented escalation ladder stages on repeated
  triggers as shipped.
- P5 `monitor.p5.v1`: identical to P4 with B2 in place of B1.

All other kernel behavior, fixtures, seeds, budgets, and normalization rules
are held constant across baseline and candidates. The shipped default remains
`monitor.p1.v1` until selection (ADR-0008 consequence). Unread job output
surfacing (run2 finding 3.2: 6 of 30 jobs never read) is recorded as a
supervision/context surfacing candidate routed to the separate context and
memory policy review; it produces no negative signal by itself and is not a
trigger in this EXP.

## Run plan (operator-approved 2026-10-30)

Any revision requires a new recorded operator approval before `running`.

Fixtures and sample (reused unchanged from EXP-0001 — no new fixture
infrastructure):

- The six labeled scripted fixture classes: `productive`,
  `plateaued-exploring`, `stalled`, `repetitive`, `regressing`, and
  `recovery-drill`; seeds 0–4 per class.
- Policies B0 (`monitor.p1.v1`), P4, and P5 paired by class and seed:
  6 × 5 × 3 = 90 scripted runs; B0 repeats once per fixture and seed
  (30 re-executions) as the determinism check; total ≤ 120 executions.
- Fixtures are deterministic scripted models and scripted environments plus a
  fixture-owned external evaluator; no provider calls and no network — the
  EXP-0001 scaffolding pattern (`tools/exp_0001_supervisor_policy.py`).

Run2 replay (computed, no provider calls):

- Each policy is replayed over a sha256-verified copy of the run2 journal
  (copy method per the attempt-2 review header; the live store is not
  opened). Pre-registered boundary expectations: B0 zero triggers; P4 — R1/R2
  at boundaries 12 and 17, B1 at calls 24, 28, 32, 36; P5 — R1/R2 at
  boundaries 12 and 17, B2 none. The replay table is compared against these
  expectations and every deviation is recorded and explained.

Budgets per fixture run (unchanged from EXP-0001):

- Main model calls ≤ 12, external actions ≤ 24, context ≤ 4096 tokens,
  evaluation requests ≤ 3; supervisor allocation ≤ 4 calls and ≤ 2048 tokens
  per run — the only permitted budget delta.
- Totals: ≤ 1440 main-call slots and ≤ 480 supervisor-call slots
  (120 executions × the per-run allocations).

Scaffolding:

- Candidate monitors live in experiment-local scaffolding over public
  kernel/journal APIs; the shipped supervision implementation stays on
  `monitor.p1.v1` until selection. The run manifest freezes fixture
  definitions, policy versions, seeds, budgets, and the exact revision used.

## Measurements

- Run2 replay: per policy, the trigger boundary table versus
  pre-registration.
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

1. Build the experiment-local scaffolding: candidate monitors P4/P5 over the
   EXP-0001 fixture harness, the run2 replay driver, and the aggregate
   calculation script.
2. Replay the run2 journal copy with B0, P4, and P5; record the boundary
   tables against pre-registration.
3. Freeze the run manifest (fixtures, policy versions, seeds, budgets,
   revision) before execution.
4. Run B0 (twice per fixture and seed) and P4/P5 (once per fixture and seed)
   over the fixed budgets; retain journals, evaluator receipts, and
   trigger-input logs.
5. Reproduce the aggregate table from the retained journals with the bundle's
   calculation script.
6. Compare measurements against the tolerances below and record the result.

## Tolerances (operator-approved 2026-10-30)

- A candidate is inadmissible if it does not fire on the run2 replay within
  its pre-registered boundaries. P5's must-fire coverage is carried by R1/R2,
  which both candidates share; its B2 component is governed by the next
  bullet.
- If P5's B2 fires on the run2 replay, its pre-registered non-fire
  expectation is falsified: the boundary and an explanation are recorded, and
  P5 remains admissible — an unexpected fire strengthens the live-sensitivity
  evidence rather than weakening it. If B2 does not fire, the pre-registered
  blindness is confirmed and enters the B1/B2 selection comparison.
- A candidate is accepted only if (a) external acceptance strictly improves
  over its paired B0 run in at least 3 of the 20 stagnation-pair comparisons
  (`stalled`, `repetitive`, `regressing`, `recovery-drill` × 5 seeds) and
  (b) actions from onset to first externally accepted recovery improve over
  B0 in at least 2 of those 4 classes — a class where B0 never recovers but
  the candidate does counts as improvement — with at least 3 of 5 seeds
  improving in each counted class.
- A candidate is rejected if any trigger fires on the 5 `productive` control
  runs; or more than one trigger fires per `plateaued-exploring` run; or
  supervisor calls or tokens exceed the allocation in any run; or any
  duplicate external effect or replay divergence appears.
- The result is inconclusive, not a pass, if any class has fewer than 5
  completed seeds, more than one censored or budget-exhausted run, or B0 does
  not reproduce across its repeat executions.
- If multiple candidates pass, select the fewest false triggers, then lowest
  supervisor cost, then lowest median trigger latency; ties by policy ID.

## Stopping rule

The EXP ends when B0, P4, and P5 are each measured against the run2 replay
and the fixture suite, or when the fixed run budget (120 executions) is
exhausted. If no candidate meets the tolerances, the result is "no policy
selected"; the shipped `monitor.p1.v1` remains, and the coverage gap stays
open with this evidence recorded.

## Artifact locations

- Seeded fixture journals, evaluator receipts, trigger-input logs, run2
  replay boundary tables, the run manifest, the runner, the replay driver,
  and the aggregate calculation script: `evidence/exp-0002-signal-coverage/`.
- The run2 journal copy method and verification hash are recorded in
  `coordination/diagnostics/acceptance-20261028/timing-accept-attempt-2-review.md`
  (header) and its `review-data/` series.
- This document's Results section records the final measurements and the
  selected policy or the rejection.

## Results

Executed 2026-10-30 from revision `328a7d2` (branch `atlas/plant-atlas`; tool
`tools/exp_0002_signal_coverage.py`; zero provider calls). The retained
bundle is `evidence/exp-0002-signal-coverage/`: 120 scripted executions
(six fixture classes × seeds 0–4 × {B0, P4, P5}, plus 30 B0 determinism
re-executions), per-run canonical event streams with retained artifact
payloads, trigger-input logs, `manifest.json`, `results.json`, `report.md`,
`aggregate.py`, and `replay/`. The run2 replay ran against sha256-verified
copies of the run2 journal
(`ca9adf8f2efdca1e5dec48de9e9d87a09081df681c82a609c83112512f2719ec`) and its
content-verified artifact store (100 objects); the copy method is the
attempt-2 review header's and the live store was not opened.

### Run2 replay versus pre-registration

| Policy | Pre-registered | Observed | Deviations |
| --- | --- | --- | --- |
| B0 `monitor.p1.v1` | zero triggers | zero triggers, at every boundary 2–40, by both the replay monitor and the shipped `evaluate_supervision` cross-check | none |
| P4 `monitor.p4.v1` | R1/R2 at boundaries 12 and 17; B1 consumed 24/28/32/36 (boundaries 25/29/33/37) | R1/R2 at 12 and 17; B1 at 25/29/33/37 | none — missing 0, extra 0 |
| P5 `monitor.p5.v1` | R1/R2 at boundaries 12 and 17; B2 none | R1/R2 at 12 and 17; B2 none | none; B2's pre-registered non-fire is confirmed, not falsified |

The replay's R1 (`stdout_strict`) fingerprints match the committed review
data (`review-data/counterfactual_a_fingerprints.csv`) on 30/30 compared
rows, byte-exact. R2 (`stdout_normalized`) matches on 5/30: the review's
non-empty normalization was computed with a session-local rule that was
never committed, and this EXP's documented rule (strip HTTP header blocks
and `---` echo-marker lines, lowercase, whitespace-collapse) agrees on the
empty outputs that carry both pre-registered R2 fires (12 and 17) while
diverging on non-empty outputs, which never repeat in run2 — the fire table
is unaffected. This is the one recorded replay deviation per the Tolerances.

### Fixture suite (120 executions)

B0 reproduced across all 30 repeat executions; no censored or
budget-exhausted run in any class; the fixed run budget was not exhausted.

| Candidate | Improved stagnation pairs | Classes ≥3/5 seeds | False triggers (productive) | False triggers (plateaued-exploring) | Supervisor calls | Median trigger latency (calls) | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P4 | 20/20 | 4/4 | 0 | 5 (1 per run) | 25 | 3 | admissible, not selected |
| P5 | 20/20 | 4/4 | 0 | 0 | 20 | 3 | **selected** |

Both candidates met every acceptance tolerance: 20/20 stagnation-pair
improvements over their paired B0 runs with at least 3/5 seeds improving in
all four stagnation classes; zero triggers on the productive control runs;
at most one trigger per plateaued-exploring run; no supervisor call or
token allocation exceeded. The predeclared selection order (fewest false
triggers, then lowest supervisor cost, then lowest median trigger latency,
ties by policy ID) selects **P5 `monitor.p5.v1`** — identical to P4 except
the claim-free B2 burn trigger — which fires on none of the ten control
runs while matching P4's recovery coverage and carrying the same R1/R2
must-fire coverage on run2.

## Interpretation and limitations

The measurement confirms every pre-registration and selects P5. The B1/B2
contrast is the substantive finding. On the run2 replay, B2's claim-free
progress counter confirms its pre-registered blindness: run2's non-empty
outputs were byte-distinct on every repeat, so first-seen evidence kept
arriving and the burn never fired. On the fixture suite, the same definition
is what removes B1's five plateaued-exploring false interventions — an
exploring plateau produces fresh evidence by definition, so the claim-free
counter correctly refuses to call it burn. The selection trades away B1's
independent burn sensitivity on evidence-churn stagnation (run2's own shape)
for zero false interventions on control runs; P5's must-fire coverage on
run2 is carried by the R1/R2 evidence fingerprints both candidates share,
and the fixture tolerances hold with margin.

The seeded scripted sample measures trigger separation on labeled fixtures,
not live-task benefit; the run2 replay is a single live instance (n=1) and
carries the false-negative claim only for the observed failure shape;
BENCH-0001 later measures live benefit and false-intervention cost. Per the
ADR-0008 consequence, the shipped `monitor.p1.v1` remains valid until a
separate recorded decision adopts this selection; switching the shipped
implementation's default policy is a kernel change outside this EXP's
experiment-local scope. Any selected policy is a versioned run policy, not
an architecture change (ARCH-0001 parameters-only delegation), and no
per-task tuning is permitted (ADR-0003). The R2 normalized-oracle deviation
above bounds the R2 claim: R2's normalization for non-empty outputs is
implementation-defined in this EXP and must be restated if a later EXP
reuses the variant.
