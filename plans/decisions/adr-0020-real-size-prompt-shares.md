---
id: ADR-0020
title: Context fit in real size, with fixed prompt shares, once a provider has reported usage
type: decision
status: accepted
owners: [operator]
created: 2026-11-17
updated: 2026-11-17
depends_on: [ADR-0008, ADR-0012, ADR-0015, ADR-0017]
supersedes: [ADR-0016]
related: [ARCH-0001, PHASE-0004, BENCH-0001]
---

# Context fit in real size, with fixed prompt shares, once a provider has reported usage

## Context and decision requested

ADR-0016 kept the whitespace word count as the measure that decides which
context fits. It added a calibrated projection only to refuse a whole
request after the fact. The ARC-AGI-3 smoke run (COD-38, retained under
`evidence/phase-0004-arc-agi-3-smoke/`) shows the cost. A 64-character hex
grid row counts as one word, so the kernel's counter reported 906 of 65,536
tokens while the provider counted 7,843 to 26,457 prompt tokens per request.
Selection therefore had no real idea how full the prompt was. More than half of
the last request (24K of 48K characters) was the context manifest's own item
lists.

COD-73 (merged in #27) made the recorded estimate an upper bound. Before the
first provider report it assumes 1.25 characters per token, below every ratio
observed so far: run6's 1.28 to 2.95, and about 1.75 for ARC hex grids. After a
report it uses the worst observed tokens-per-character ratio with a 10% margin.
The same change sent the model per-band item counts instead of the growing
item lists. This ADR uses that estimate to decide fit.

ADR-0016 also records why fit cannot move to real size before the first
provider report. The frozen EXP-0002 and EXP-0003 windows are 4,096 tokens,
and their mandatory context is about 12,100 characters, so any honest
character-based bound rejects them. Scripted runs report no usage and must
stay reproducible.

The operator approved this direction on 2026-11-17, after a review of the
smoke run proposed a single budget design and a second review corrected it.
This change affects every environment, not only ARC-AGI-3.

## Decision

### 1. The fit basis switches at the first provider report

Before any provider has reported prompt tokens in a session, fit is decided in
the recorded word count, exactly as under ADR-0016. From the first report on,
fit is decided in the calibrated estimate (`calibrated-chars-v2`, the worst
observed ratio plus 10%). The total that must fit is the request, plus the
system prompt, plus the reserved output.

Each context manifest records its `fit_basis` (`whitespace-v1` or
`calibrated-chars-v2`). The word counts (`serialized_input_tokens`,
`tokens_used`, the per-band `allocation`) are still recorded as diagnostics.
In calibrated mode they no longer decide anything.

### 2. Fixed shares, in the calibrated estimate

In calibrated mode, each request is split as follows:

| Share | Limit |
| --- | --- |
| Must-have (objective, contract, latest result, current observation, and the rest of the mandatory band) | Always included. If it does not fit, the request is refused as a context overflow before dispatch. |
| Bookkeeping (the manifest summary sent to the model) | A fixed 256 tokens, reserved because it is filled in after the candidate items are measured. It holds counts only. Item lists stay in the journal. |
| Retrieved notes | At most 25% of what is left after must-have, bookkeeping and reserved output. |
| Recent history | Everything else, including any of the retrieved share that notes leave unused. |

The manifest records the four limits as `share_limits`. Under the word count,
`share_limits` is empty and every band may use the whole budget, as before.

### 3. The worst-seen ratio never comes back down

Calibration keeps the worst ratio seen in the session. One unusually dense
call therefore raises the estimate, and shrinks the usable prompt, for the
rest of the run. This is accepted. An estimate that can come in under the
provider's count is not a bound, and a budget needs a bound.

The cost is measured. In a 200-decision run with a 16,384-token budget,
reporting 0.3 tokens per character:

| Run | Must-have at the end | Room for history and notes | Request size at the end |
| --- | --- | --- | --- |
| No dense call | 2,493 | 13,635 | 5,293 |
| One call at 0.9 tokens per character (decision 3) | 6,963 | 9,165 | 14,658 |

Every request in both runs fit, with no refusals. A campaign should expect
this effect and set its budget with the worst content it will see in mind.

### 4. Clauses of ADR-0016 that continue

- Once a provider has reported usage, a request whose projection with the
  worst observed ratio exceeds the budget is refused before dispatch, with a
  `context_overflow` event.
- Provider-reported input plus reserved output above the budget is journaled
  as a `token_budget_breach` negative signal (ADR-0008). It never triggers an
  automatic retry and never passes silently.
- Calibration is run-local. It is derived inside one session from the
  journal's intents and terminals, and it is not shared across runs or
  persisted as policy.

### 5. Results are not comparable across this change

A run under this ADR selects different context from a run under ADR-0016 as
soon as a provider has reported usage. Results from before and after this
change must not be pooled or compared as one condition. Each manifest's
`fit_basis` and `token_estimator` say which rule produced it. A campaign
manifest names this ADR among the rules its runs follow.

## Consequences

- Live runs decide fit in a measure close to what the provider bills, from
  their second call on. The first call still uses the word count, and the
  ADR-0016 projection still guards it once a report exists.
- Scripted runs, which report no usage, are unchanged. The frozen
  EXP-0002/EXP-0003 evidence stays reproducible.
- Journals written before this change replay unchanged. Replay does not
  re-render requests, and the new manifest fields are recorded, not derived.
- A request refused because its must-have content does not fit in real size
  now fails at assembly ("mandatory context exceeds token budget") rather
  than at the later projection. The event and the refusal are the same kind.
- Retrieved notes can no longer crowd out recent history in calibrated mode.

## Implementation and acceptance boundary

`context.py` holds the fit basis on `TokenAccounting`, `RETRIEVED_SHARE`,
`BOOKKEEPING_TOKENS`, the `_shares` split, and the `fit_basis` and
`share_limits` manifest fields. `session.py` sets the fit basis from the
journal's provider reports. `kernel.py` sends included and omitted items to
the model as counts. Tests cover:

- the switch at the first report;
- the share arithmetic;
- a 100-decision run with one dense early call, checking that the ratio
  never decreases and every request fits;
- the calibrated refusal;
- replay.

Linear COD-85.

## Dependents updated in this same change

ADR-0016 is marked `superseded`. ARCH-0001's context section states the
switch and the shares. The index gains this row, and WORK-0001 records the
change.
