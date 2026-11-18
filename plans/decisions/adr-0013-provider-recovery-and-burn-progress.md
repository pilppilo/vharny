---
id: ADR-0013
title: Provider recovery and supported-progress budget checkpoints
type: decision
status: accepted
owners: [operator]
created: 2026-11-06
updated: 2026-11-16
depends_on: [ADR-0019, ADR-0008]
supersedes: [ADR-0011]
related: [ARCH-0001, EXP-0002, EXP-0003, PHASE-0004, BENCH-0001, SCHEM-0001]
---

# Provider recovery and supported-progress budget checkpoints

## Context and decision requested

The operator requested corrections for both retained Timing attempt-4 failures.
Evidence is frozen at `a80872c`,
`coordination/diagnostics/acceptance-20261028/timing-accept-attempt-4.md`:
the provider failed at decision 26 with no usable output, and the P5 burn
trigger never fired despite sustained reconnaissance. Attempt 1 previously
ended in the same transport class. The firewall-refresh correlation is not a
proven cause and does not determine the repository fix.

ARCH-0001 requires bounded retries and supported outcome or knowledge progress.
ADR-0011 explicitly accepted P5's weaker burn coverage in exchange for fewer
false interventions on exploration. EXP-0002 predicted B2's novelty blind spot.
Receipt fingerprint journaling repaired evidence delivery; it did not repair
that policy trade-off. Merely reverting to P4 repeats its measured exploration
interventions. This proposal makes that cost explicit and measures it again.

This document is a proposal, not approval to switch policy or launch a run.
ADR-0011 remains effective until this proposal is accepted and its gated
replacement is adopted. EXP-0003 is a draft requiring approval before execution.

## Proposed decision

### Bounded provider recovery

1. Persist versioned retry settings with the run/model configuration: at most
   two additional transport attempts per logical decision, with delays of two
   and eight seconds. Keep the configured per-call timeout; derive and expose
   the maximum recovery duration. Do not change JSON repair or output budgets
   implicitly. Exhaustion of either retry capacity or resource capacity ends
   automatic recovery for that decision.
2. The coordinator schedules each retry only after settling the previous
   attempt and processing queued controls. Backoff uses a deadline and a
   responsive coordinator, not a sleep inside the writer or an adapter loop.
   Each dispatched provider call has its own durable identity, reservation,
   measured/unknown usage, duration and sanitized failure classification, linked
   to its logical decision. Retries consume the existing call budget. Unknown
   token cost stays unknown; a timeout never establishes zero provider cost.
3. Retry only explicitly classified transient connection/timeouts, rate limits,
   and transient server failures. Authentication, permission, invalid request,
   configuration errors, output exhaustion and malformed JSON are distinct
   outcomes; the existing repair path handles malformed JSON. An unclassified
   failure is retained for the operator rather than guessed retryable. Provider
   exception text, keys and request headers never enter journal diagnostics.
4. Recovery applies to stateless text-completion requests with no provider-side
   tool execution. Duplicate remote generation or billing remains possible and
   is disclosed as uncertain exposure. It never retries an external action,
   evaluation or unknown runtime operation. SDK/legacy automatic retries and
   caches cannot conceal extra calls.
5. Before each retry, recheck objective, attempt, lineage, pending controls,
   remaining budget and decision applicability. Pause and stop prevent another
   call. Steering invalidates the old decision; late output is retained and
   accounted but never applied to a new objective. Restart does not blindly
   resend an unresolved provider intent.
6. When automatic recovery is exhausted, record an explicit provider-recovery
   waiting condition and expose it through status/CLI. The operator may resume
   with remaining budget after fixing availability; waiting burns no calls.
   Authentication/configuration failures expose an operator-action condition
   immediately. Stop remains terminal. Corruption/integrity failures retain
   their existing fail-stop behavior.
   *Amended by ADR-0024 (2026-11-18): a retry the budget cannot pay for ends
   the run with a `budget_exhausted` outcome instead of this wait.*

### Supported-progress budget checkpoints

1. Replace P5's burn-reset assumption in a separately versioned candidate
   `monitor.p6.v1`: a new evidence digest is evidence arrival, not by itself
   supported progress. Preserve R1/R2 fingerprints for repetition detection.
2. A burn checkpoint becomes due after `ceil(0.6 * model_call_budget)` consumed
   calls without newly supported, current-objective outcome/knowledge progress.
   At a budget of 40 and no such progress it is due immediately after call 24
   settles, before call 25 is scheduled. Use settled ledger call consumption,
   including retries and supervisor calls charged to that budget, rather than
   the count of decision manifests. Progress resets the consumed-call anchor;
   raw receipts, new logs, recovery, pending/self-reported claims and novel
   fingerprints do not. A valid supported progress event must retain its
   existing scope, evidence and assessor checks; this adds no local grader.
3. Preserve the existing four-call cooldown, shared repetition precedence and
   guidance/replan/operator-question escalation. A checkpoint is an advisory
   spending review, not a verdict that exploration failed or that the objective
   is complete. If no supervisor call fits the remaining budget, expose a
   durable operator question with the trigger evidence instead of silently
   skipping the checkpoint. No trigger silently terminates the task.
4. Journal policy version, cause, consumed-call count, threshold, progress
   anchor and supporting event IDs, and cooldown/suppression disposition.
   Expose novelty and supported progress separately in diagnostics. Replay
   must reproduce every due checkpoint and its disposition. Historical P5
   journals retain P5 semantics and are never rewritten as P6 successes.
5. EXP-0003 must measure run4 sensitivity and the exploration intervention cost
   before adoption. Zero exploration interventions is no longer the overriding
   selection preference: novel-output burn must not pass silently. The operator
   reviews measured overhead and benefit before a separate default switch.

## Implementation and acceptance boundary

Transport: typed classifications at the model boundary (`kernel.py` and the
provider adapter), scheduling/backoff in `application.py`, durable intent and
settlement in `session.py` through existing transitions, and persisted settings
in run configuration. Workers perform one bounded call and never schedule
retries. CLI remains presentation/configuration only. The synchronous embedding
API must expose the same bounded outcome without hidden retries.

Supervision: experiment-local P6 first; `supervision.py` owns deterministic
signals, `memory.py` the supported-progress projection, and the existing session
path journals and applies triggers. No benchmark identity selects behavior.

Required transport tests: transient failure then success; repeated failure and
budget exhaustion; nonretryable errors; unknown usage; failure during JSON
repair; pause/stop/steer during call and backoff; restart between intent/result;
late results; no duplicate runtime submission; status exposes recoverable wait.
Required supervision evidence is specified by EXP-0003. Run the repository's
full verification once against committed implementation under `baton:verify`.

## Dependents and adoption sequence

On operator acceptance, update ARCH-0001's Failure and recovery behavior and
Progress, supervision, and variation sections, plus SCHEM-0001's affected
failure/control paths, in the same governed adoption change. Reassess
PHASE-0004 readiness and BENCH-0001 failure-injection coverage; their required
families and phase status do not change merely because fixes are proposed.
Update affected proto contracts for typed recovery outcomes and persisted
settings. Accepted ADR-0011's historical text is immutable.

Then implement and verify transport recovery, execute the approved EXP-0003,
present its measured result, and obtain policy-adoption approval before
switching the shipped monitor. No live model calls are necessary for either
offline change. A later live acceptance requires a fresh predeclaration and
gate check 12 reconciled with retained run4. This proposal authorizes no merge
to `next`, entrypoint switch, host firewall change, or attempt 5.

## Alternatives and limits

Unlimited retries conceal outages and consume budget; terminal failure on the
first transient disconnect repeats the observed defect. Bounded retries plus
an observable recoverable wait limit both problems. They do not guarantee
provider availability or undo unknown remote cost.

More aggressive fingerprint normalization cannot establish objective progress
from arbitrary output. P5 remains the experimental baseline; P4 remains the
known claim-based comparator. P6 explicitly accepts advisory exploration
checkpoints as a cost to measure. It does not claim that supported-progress
bookkeeping is complete, or that live benefit has been demonstrated.
