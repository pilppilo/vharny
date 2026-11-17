---
id: ADR-0021
title: Ask for the answer before raising the answer limit
type: decision
status: accepted
owners: [operator]
created: 2026-11-17
updated: 2026-11-17
depends_on: [ADR-0008, ADR-0017, ADR-0020]
supersedes: []
related: [ARCH-0001, PHASE-0004, BENCH-0001]
---

# Ask for the answer before raising the answer limit

## Context and decision requested

ADR-0017 answers a decision that runs out of answer tokens with one re-ask.
The re-ask is sent at the same limit and spends one unit of the repair budget.
It asks for the answer now, without more reasoning. ADR-0017 §2 kept the
answer limit fixed within a decision, because the limit and the context's
reservation were one contract, and left raising it to a later change.

COD-88 made that change, as an opt-in. `--initial-output-tokens N` starts each
decision at N tokens; N is also what the context reserves. When a call runs
out, the request is re-sent at double the limit. Each attempt records the
limit it asked for (`output_limit`), and the provider budget check uses the
largest limit actually requested. Growth stops at the first of these:

- `--max-output-tokens`;
- what the token budget still fits, given the provider's reported prompt size;
- the point where the remaining model calls are needed for the repair budget.

The LongCat-2.5 trial on two development games
(`evidence/phase-0004-longcat-trial/`) ran that growth from 4,096 to 32,768
tokens. Three findings matter:

- **The model starts over on every raise.** It doesn't continue reasoning
  where it was cut off.
- **Reasoning length varies from try to try.** The same decision needed very
  different lengths on different tries. One ran out at 16,384 tokens, then
  answered at the next limit in 1,591.
- **Asking for the answer worked where raising failed.** ADR-0017's re-ask
  saved a decision after every raise had failed: 44 s, 2,755 tokens.

Calls that ran out took 91.2 minutes; calls that answered took 7.2. Raising
the limit with the bare request mostly paid for longer restarts.

The operator approved this change on 2026-11-17 by answering "yes" to taking
up COD-91, whose acceptance criteria ask for this order and for this note.

## Decision

### 1. Growth itself (COD-88), recorded here

Growth is opt-in through `--initial-output-tokens`. Without it, nothing in
ADR-0017 changes: one fixed limit, one budgeted re-ask, and no `output_limit`
on attempts. With it, the reservation is the starting limit. The run identity
and the model configuration record the setting only when it is set, so other
runs keep their identities.

### 2. With growth on, ask for the answer first

When a call runs out, the next call is ADR-0017's answer-now re-ask at the
same limit. It is sent once per decision, and it doesn't spend the repair
budget. It is still a reserved model call, recorded as an attempt with a
`model_repaired` note:

- field `output-ceiling`;
- marker `answer-now re-ask at output limit N before any raise; no new
  context`.

The re-ask is sent only when the remaining model calls still leave the repair
budget its calls.

### 3. Raises keep asking for the answer

If the re-ask also runs out, the limit doubles under the stopping rules above.
Each raised call carries the same answer-now instruction, not the bare request.
Each raise is recorded with field `output-limit` and the marker
`raised output limit A -> B; answer-now re-ask`.

### 4. The budgeted re-ask stays last

When no raise is left, ADR-0017's re-ask runs as before: it spends the repair
budget, keeps its marker, and is followed by terminal `output-exhausted` if
it runs out too.

## Why the free re-ask rather than a setting

The criteria allowed either: a free re-ask with growth on, or a setting that
chooses the order. A setting would add a campaign variable with no case for
the old order. The trial shows the bare re-send at a larger limit costing the
most, so the free re-ask is chosen. Its call is still bounded by the
model-call budget, and every call is recorded.

## Consequences

- With growth on, a decision whose re-ask answers costs two calls instead of
  four or more.
- A decision that runs out at every limit now costs one more call: the free
  re-ask comes before the raises. That call is bounded by the same budget
  checks.
- Results from runs with growth on before this change used the bare request
  on raises. They are not comparable with later ones and must not be pooled.
  The only such runs are the trial's.

## Dependents updated in the same change

- ARCH-0001 'Failure and recovery behavior': the ADR-0017 paragraph gains the
  growth order.
- The index gains the ADR-0021 row.
- WORK-0001 records the decision.

ADR-0017 stays accepted: its re-ask and its terminal outcome are unchanged,
and §2's fixed limit holds whenever growth is off.

## Alternatives considered

- **Raise with the bare request (COD-88 as merged).** Rejected on the trial
  evidence above.
- **Never raise; re-ask only.** Kept possible, by leaving growth off. Raises
  still help a model whose answer truly needs more room. Both raises and the
  re-ask are bounded.
- **Make the order a setting.** Rejected above.
