---
id: ADR-0016
title: Provider token accounting and context-fit enforcement
type: decision
status: superseded
owners: [operator]
created: 2026-11-09
updated: 2026-11-17
depends_on: [ADR-0008, ADR-0015]
supersedes: []
related: [ARCH-0001, PHASE-0004]
---

# Provider token accounting and context-fit enforcement

## Context and decision requested

ADR-0015 rung 2 is gated on provider-reported usage and context-fit
enforcement. Run6 (2026-11-09) shows why. The runtime counted
whitespace-delimited words: the manifest recorded 733-2,583
`serialized_input_tokens` across 21 decisions while the provider reported
3,700-23,936 prompt tokens, and a 205,811-character request (a 182,872-byte
tool observation admitted whole) was counted as about 1,650 tokens while the
provider charged 160,652. The declared settings were `--token-budget 24000
--max-output-tokens 16384`. Admission used the word count, so over-budget
calls were sent silently; provider usage was journaled but never reconciled
against the budget. The word count is a usable cheap relative measure for
band selection; it is not an admission measure once a provider has reported
actual usage.

A first design made the admission bound a fixed conservative estimate. Its
gate (commit `1c9ec6b`, retained as a failed attempt) showed why that fails:
the frozen EXP-0002/EXP-0003 windows are 4,096 tokens, and mandatory context
(result schemas plus system prompt) measures roughly 12,100 characters, so
any honest fixed bound rejects it, while the word count had reported 760
tokens. A fixed divisor cannot both bound dense content and fit the run
windows the repository's completed evidence was produced under.

The operator ordered this repair on 2026-11-09 as follow-up 4 of the
recorded list. This ADR records the accounting decision; rung 2 itself
remains ADR-0015's.

## Decision

### 1. Recorded counters keep their roles

`serialized_input_tokens`, `system_prompt_tokens`, `tokens_used`, and
`token_counter` keep their existing meaning: the recorded serialized window
and the band-selection heuristic. They remain what the assembler uses to fit
droppable context and what older evidence was produced under.

The manifest additionally records `estimated_input_tokens` (a coarse
`conservative-chars-v1` bound of three characters per token) and
`token_estimator`. These fields are diagnostic; they do not decide
admission on their own.

### 2. Enforcement is calibrated from provider reports

Before the first provider report the runtime cannot know a provider's
tokenizer; it keeps the recorded-counter admission behavior. Once a provider
has reported prompt tokens for a serialized request in this run, the
runtime holds the worst observed tokens-per-character ratio and projects
each candidate request with it. When `projected input + reserved output >
token_budget`, the session persists a `context_overflow` event and refuses
the call with "projected context exceeds token budget" before dispatch. The
projection always uses the worst ratio seen, so a denser later payload can
only become more conservative.

### 3. Provider-reported usage is reconciled, and breaches are visible

Every provider result already records reported prompt and completion tokens.
That report is compared inside the decision's atomic batch: when
`provider input + reserved output > token_budget`, the session journals a
`token_budget_breach` event with the request id, the estimate, the reported
input, the reserved output, and the budget. The event changes no projection
and is a negative signal in the ADR-0008 menu sense; it never triggers an
automatic retry and never silently passes.

### 4. Calibration is run-local and provisional

The ratio is derived inside one session from the journal's intents and
terminals; it is not shared across runs and is not persisted as policy. A
later change may refine the projection (for example with a provider-reported
tokenizer identity or a safety margin); until then the worst observed ratio
governs.

## Consequences

- Re-simulating run6 under this decision: the first decision is admitted
  before any report; from the first report on, the observed ratios (about
  0.34-0.44 tokens per character) project call 4's 23,146-character request
  at about 10,100 input tokens, plus 16,384 reserved, above the 24,000
  budget, so the run would have been refused at that call instead of
  silently overspending. The 205,811-character request projects far above
  the budget and is refused.
- Scripted runs whose models report no usage (the frozen EXP-0002/EXP-0003
  evidence) keep their existing admission behavior; their completed evidence
  stays reproducible.
- The gate6 preflight's context-fit check reads `serialized_input_tokens`
  and remains a pre-provider approximation; the first live call can still
  exceed the window, and a breach is then journaled rather than hidden.
- Re-pinning the 24,000/16,384 run settings remains the operator's decision.

## Implementation and acceptance boundary

Accounting lives in `context.py` (estimator and manifest fields),
`session.py` (calibrated projection and breach journaling), and `replay.py`
(the breach event validates without changing a projection). Tests carry the
run6 request shapes as fixtures and prove the calibrated refusal, the breach
event, and replay acceptance. Full verification runs once against the
committed change under `baton:verify`; the failed `1c9ec6b` gate console is
retained.

## Dependents updated in this same change

ARCH-0001's context section gains the admission and reconciliation
invariant. The index gains this row. WORK-0001 records the change.
PHASE-0004 status is unchanged.
