---
id: ADR-0024
title: Whole-run token and time limits, and a budget-exhausted outcome
type: decision
status: accepted
owners: [operator]
created: 2026-11-18
updated: 2026-11-18
depends_on: [ADR-0013, ADR-0016, ADR-0020, ADR-0021, ADR-0022]
supersedes: []
related: [ARCH-0001, PHASE-0004, BENCH-0001]
---

# Whole-run token and time limits, and a budget-exhausted outcome

## Context and decision requested

A run could be limited by game actions, model calls and evaluations. A
campaign also needs limits on billed provider tokens and on time, set before
the run, and needs to tell "the budget ran out" apart from "the agent failed".

Before this change, running out of a budget surfaced in three different ways:

- A model-call budget raised a plain transition error. The CLI reported it as
  a failure and the run stayed resumable, although resuming could not help.
- During a transport retry, the same exhaustion entered ADR-0013's
  provider-recovery wait. That wait is for an operator to fix availability,
  which cannot help either.
- When no action and no evaluation could be paid for, the run ended with
  "no dispatchable work remains".

`BudgetLimits.cost` existed with no way to set it.

The operator implemented the first slice on 2026-11-18: the outcome event,
the time limit and the token ledger. They approved the completed version the
same day (Linear COD-86).

## Decision

### 1. One outcome for every budget

When a limit refuses work, the session journals `budget_exhausted` with the
resource and a reason, for example `{"resource": "token", "reason": "token
budget exhausted"}`, and ends `stopped`. The resources are:

- `action`
- `model call`
- `evaluation`
- `action and evaluation`
- `token`
- `time`
- `cost`

Replay accepts the event only for one of these names, with the matching
reason, from a non-terminal state. The CLI reports `TERMINAL budget
exhausted; <reason>` and exits 1. `status` shows the event in its stream. It
is neither a failure nor an operator stop.

ADR-0013 §6 changes for one case. If a retry is due and the budget can't pay
for it, the run now ends with this outcome instead of entering the
provider-recovery wait: resuming cannot add budget.

### 2. A wall-clock limit

`--max-wall-clock-seconds` sets the run's time in seconds. Time is counted
from the session's first journal event, so it is elapsed time: it keeps
running while the run is paused or its process is down, and a resumed run
keeps its original deadline. The limit is recorded in the run manifest, the
task budgets and the run identity, each only when set.

The coordinator checks it on every tick. If the deadline passes while a
decision is waiting for the model, the decision is recorded as failed and
discarded, its reserved calls and tokens stay charged, and the run stops with
`time`. The provider call itself is not cancelled: it may keep its worker busy
until it returns, which delays shutdown by up to the provider timeout.

Elapsed time is chosen over active time because a campaign limit bounds real
time and spend, and elapsed time is simple to audit. The campaign manifest
has to size the limit for its failure-injection game, whose downtime counts.

### 3. A token limit

`--max-tokens` limits billed tokens for the whole run. Billed means prompt
plus answer, as the provider reports them. Reasoning tokens are part of the
answer and are never added again.

Before each model call, the session reserves the worst case:

- **A decision:** its maximum number of calls × (calibrated prompt estimate +
  largest answer). The maximum number of calls is the first call, plus the
  repair budget, plus, with answer-limit growth on, the free answer-now
  re-ask and one call per doubling (ADR-0021).
- **A supervisor consult:** its digest and system-prompt estimate, plus its
  answer limit.

The call is refused, ending the run with `token`, if the reservation doesn't
fit. After the call, the reservation is settled to the provider's reported
use; unknown usage keeps the reservation. A run can therefore not exceed its
token limit.

Before the provider's first usage report, the prompt estimate uses ADR-0020's
conservative prior, so the first reservation is large.

Reservations carry `tokens` only when the run has a token limit.

### 4. Old journals keep their shape

The new optional fields are left out of the encoding while unset: the
budgets' `tokens` and `wall_clock_seconds`, and a reservation's `tokens`.
Journals, projections and retained evidence recorded before this change keep
their digests. The codec supports this for any field marked `OMIT_IF_NONE`.

### 5. A cost limit

`--max-cost USD` limits the dollars of provider spend for the whole run. It
needs `--rate-card FILE`: a JSON object with `input_per_million`,
`output_per_million`, optional `cached_input_per_million` and `label`, in
dollars per million tokens. Rates are operator-supplied, never discovered:
provider list prices can be promotional and the provider's billing record stays
authoritative. The card's contents, not its path, are recorded in the run
manifest and the run identity, each only when set, so a reopened run prices at
the same rates.

- **Pricing a call.** Uncached prompt tokens at the input rate, cached prompt
  tokens at the cached rate, answer tokens (reasoning included) at the output
  rate. The cache split comes from `prompt_tokens_details.cached_tokens`. If any
  call of a decision does not report it, or reports more than the prompt, every
  prompt token is priced as uncached, an upper bound.
- **Reserving.** A decision reserves its worst case as in §3, priced with no
  cache hits. A supervisor consult reserves its prompt estimate plus its answer
  limit. A call is refused with `cost` if the reservation does not fit.
- **Settling.** A call whose cost could not be priced keeps its reserved worst
  case, as unknown token usage keeps its reservation.
- **Non-model operations.** The ledger refuses any reservation while any
  settled operation has an unknown cost. Actions and evaluations incur no
  provider spend, so when a cost limit is set they reserve and settle at zero.
  This applies only when `limits.cost` is set, so journals without a cost limit
  replay as before.

Provider prices change. Use a token or time limit as the primary bound when the
rate is promotional; the dollar limit is only as good as the card.

## Not decided here

- **Remaining tokens and time:** the model's remaining budgets don't show
  them yet, so there is no warning at 80%.
- **Cancelling an in-flight call** when the deadline passes.

The two that remain stay open in COD-86; cost is decided in §5.

## Dependents updated in the same change

- ARCH-0001's failure and recovery section and `depends_on`.
- ADR-0013 §6 gains a note pointing here.
- The index row.
- WORK-0001.

## Alternatives considered

- **Active time instead of elapsed time.** Rejected for now, as above.
- **A failure outcome for budget exhaustion.** Rejected: running out is the
  limit working, not the agent failing, and BENCH-0001 must count the two
  apart.
- **Reserving one call's worth of tokens.** Rejected: a decision can make
  several calls, so the limit could be overshot by a whole decision.
