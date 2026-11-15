---
id: ADR-0017
title: Bounded coordinator-owned re-ask for an empty decoded decision
type: decision
status: accepted
owners: [operator]
created: 2026-11-10
updated: 2026-11-11
depends_on: [ADR-0008, ADR-0015, ADR-0016]
supersedes: []
related: [ARCH-0001, PHASE-0004, SCHEM-0001]
---

# Bounded coordinator-owned re-ask for an empty decoded decision

## Context and decision requested

Fresh-series attempt 4 (`run8`, 2026-11-10) ended at decision 38 with
`finish_reason=length`, `completion_tokens=16384` (the frozen
`--max-output-tokens`), and empty visible content; attempt 5 (`run9`) ended
the same way at decision 22 with 23,572 prompt tokens, 16,384 completion
tokens, and an empty answer after 235 s. In both cases the provider returned
the model's reasoning in a separate `reasoning_content` field: the reasoning
budget consumed the whole visible allowance, no action was emitted, and the
coordinator settled the decision as terminal `output-exhausted`. The attempt
lost its remaining budget (run8: 2 calls and 4 actions left; run9: 18 calls
and 19 actions left).

Evidence, all committed: `coordination/diagnostics/acceptance-20261028/timing-fresh-run8.md`,
`timing-fresh-run9.md` and its transcript; the per-call usage in both
journals. The provider accepts `max_tokens` 32768 and 65536
(`timing-output-budget-probe-20261110.log`), and replaying run9's exact
stored failing input at the unchanged 16384 cap produced a valid action, so
the exhaustion is stochastic rather than deterministic
(`timing-output-budget-replay-20261110.log`). The operator raised the
attempt window to 96000/32768 for attempt 6; that is headroom, not a fix.

Three pieces of the current implementation matter. The compatible-model
adapter deliberately sets `length_retries=0` so the coordinator owns retries
and every provider call carries a reservation (`CompatibleModel.retry_settings`
records `coordinator_owned: true`). An empty decode raises
`_ProviderOutputExhausted` and settles immediately as `output-exhausted`; no
repair is attempted, although `repair_budget` (1) is otherwise honoured for
malformed output. And the output ceiling is bound to the decision's
reservation: `CompatibleModel` refuses a generator whose `max_tokens`
disagrees with `reserved_output_tokens`, so a mid-decision doubling would
change the reserved window. ADR-0015 §4 explicitly left `output-exhausted`
with its existing behaviour; ADR-0016 governs how the extra call's usage is
recorded.

The operator directed the drafting of this option on 2026-11-10 after
reviewing the reasoning-token metadata change, and accepted it on 2026-11-11
with the words "yes and we need to make sure our corrections work after you
implement then move on to merging into atlas". Implementation is authorized
by that acceptance.

## Decision

### 1. One bounded re-ask inside the existing repair budget

When a decision decodes empty with a length-limited finish reason
(`output-exhausted`), the coordinator spends one unit of the existing
`repair_budget` on a re-ask of the same decision instead of settling
immediately. The re-ask is a normal reserved model call: it consumes a model
call and its provider-reported input and output tokens, is journaled as a
second `ModelAttempt` on the same `model_decided` terminal, and therefore
stays inside ADR-0016 accounting exactly as the malformed-output repair does.

### 2. The re-ask is self-contained and bounded

The re-ask re-sends the decision request with a compact suffix carrying the
prior attempt's `finish_reason`, its `reasoning_tokens` count when reported,
and the instruction to emit the complete action object now without extending
reasoning. It carries no new context, invents no result, and grants no new
action. It is issued at the decision's existing reserved output ceiling; the
ceiling is not raised mid-decision, because the ceiling and the reservation
are one contract. Raising the ceiling is a separate, later change (see
§5) if the same-ceiling re-ask proves insufficient in live attempts.

### 3. The re-ask is visible, never silent

The application journals a `model_repaired` note in the same atomic batch as
the terminal, with `failure_class` `output-exhausted`, result kind `none`,
field `output-ceiling`, and a deterministic marker naming the bounded
re-ask. The existing replay branch already accepts that note shape and
changes no projection, so replay reproduces the decision deterministically
and ADR-0008's degradation-visibility rule holds: a provider that keeps
truncating reasoning stays visible as clustered `model_repaired`
occurrences, not as a silently healthier run.

### 4. Exhaustion keeps the current outcome

If the re-ask is also empty or length-limited, the decision settles as
terminal `output-exhausted` exactly as today, with both attempts and their
reasoning counts retained. This decision does not convert the class into an
operator-resumable wait; that is a separate question for the operator, and
ADR-0015 §4's carve-out for `output-exhausted` stands until a later ADR
changes it.

### 5. What stays unchanged

The provider adapters keep `length_retries=0` and never retry on their own.
The output ceiling, `max_output_tokens`, the run's token budget, the monitor
policy, and the validate-then-apply order are unchanged. No benchmark
identity selects the retry; it is uniform across environments. A non-empty
but malformed answer still takes the ADR-0015 ladder, not this rung.

## Implementation and acceptance boundary

Decode trigger and re-ask in `kernel.py` (typed result; no I/O); repair-note
journaling in `session.py` through the existing `model_repaired` path;
presentation in `cli.py` and `runview` reads the second attempt's
`reasoning_tokens`, which is already recorded. Tests carry run9's
event-274 payload as the fixture: one re-ask spent at the same ceiling, both
attempts retained with reasoning counts, the marker journaled, replay
acceptance of the note, and no re-ask when the repair budget is exhausted or
the failure is not length-limited. The repository's full verification runs
once against the committed change under `baton:verify`.

## Dependents to update in the same change

ARCH-0001 'Failure and recovery behavior' gains one paragraph: a
length-limited empty decode is re-asked once inside the repair budget at the
reserved ceiling, journaled as a repair note, and terminal after exhaustion.
SCHEM-0001 renders no decode or repair path (the ADR-0015 review found the
same), so no schematic row changes. The index gains the ADR-0017 row;
WORK-0001 records the decision and refreshes the Current handoff. PHASE-0004
status is unchanged: this is runtime hardening inside the phase's
implementation sequence and keep-all-campaigns scope.

## Alternatives considered

- Raising `--max-output-tokens` further. Already done for attempt 6; each
  raise buys headroom against a stochastic failure and cannot guarantee that
  a reasoning run never fills the allowance.
- A generator-level length retry (`length_retries > 0`). Rejected: it spends
  an unreserved provider call, so the journal and the budget would disagree,
  which is precisely why the adapter disables it.
- Doubling the ceiling inside the re-ask. Deferred: the ceiling is bound to
  the reservation (`reserved output tokens must match generator max_tokens`),
  so it needs a per-call ceiling contract and its own accounting review.
- Resuming after exhaustion. A reasonable later rung; it changes the
  ADR-0015 §4 carve-out, so it needs its own decision.
