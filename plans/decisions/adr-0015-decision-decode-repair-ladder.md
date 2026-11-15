---
id: ADR-0015
title: Decision decode repair ladder (observational recovery; operator-resumable malformed-output waits)
type: decision
status: accepted
owners: [operator]
created: 2026-11-09
updated: 2026-11-09
depends_on: [ADR-0008, ADR-0013]
supersedes: []
related: [ARCH-0001, PHASE-0004, SCHEM-0001]
---

# Decision decode repair ladder

## Context and decision requested

Fresh-series attempt 2 (run6, 2026-11-09) launched on the operator's word and
terminated at decision 23 of 40 while the attempt still had budget: console
`coordination/diagnostics/acceptance-20261028/timing-fresh-run6-console.log`
L94-L96, journal event 260 of `/home/vhrun/runs/htb-timing/run6` with both
attempts retained. The provider returned a syntactically valid,
schema-shaped `{"kind":"ActionChoice","action_name":"job_read",
"arguments":{...}}` twice, omitting only the required `rationale`; the
runtime's bounded repair round replayed the full request with a
malformed-JSON hint, received an identical semantic answer, then classified
`malformed-output` and the run ended as a runner failure. The run itself was
honest but drifting: reconnaissance-only, no plan, no evaluation request.

Three mechanics contributed. First, one contract violation is fatal at the
decode boundary, so a missing annotation field kills the attempt. Second,
the existing repair round is blind and uneconomical: it re-sends the whole
request (47,789 measured input tokens on that decision) with a hint naming
only malformed JSON. Third, the same journal shows a 160,652-token provider
call admitted against a declared 24,000-token budget while the coordinator
reported a constant 7,455 believed input tokens; provider accounting is
unreconciled today. That accounting defect is a separate, separately gated
change: the ladder below must not depend on believed token counts.

AVO framing constrains the answer. The journal keeps unsuccessful provider
output as durable evidence and never fabricates intent; committed lineage is
never extended by unwitnessed model text; the model still chooses what to
do, and the coordinator only repairs its own bookkeeping; and ADR-0008
requires provider-conformance degradation to stay visible as a negative
signal rather than be repaired silently. ARCH-0001 already requires typed
failure classes with bounded retry budgets, and ADR-0013 clause 6 already
established the durable operator-resumable waiting outcome for exhausted
recovery; this ADR extends that existing decision sentence to the
`malformed-output` class and adds a decode-time observational repair that
costs no provider call.

The operator reviewed this analysis and accepted the direction on
2026-11-09 with the words 'i agree. lets get started with that'. The
accepted scope is rungs 1 and 3 now; the compact pinned re-ask (rung 2) is
proposed for a later, separately gated change.

## Decision

### 1. Defect classification

The decode layer classifies result-schema defects instead of collapsing
them into one fatal error. Recorded classes: `unparseable` (no JSON object
found), `kind-unsupported`, `operational-field` (a required field that
determines what will execute or be retained is missing or invalid),
`observational-field` (a required annotation about the model's reasoning is
missing), and `invalid-arguments` (arguments violate the action's argument
contract). Required field annotations per result kind, effective for this
ladder: `rationale` is observational for `ActionChoice`, `EvaluationChoice`,
and `CompletionProposal`; `reason` is observational for `WaitChoice` and
`GuidanceResponse`. `GuidanceResponse.decision` remains operational even
though its kind also carries an observational `reason`. Every other
required field, and every field type, contract, and enum, is operational
and is never repaired or backfilled.

### 2. Rung 1 — observational repair in place (this change)

When the parsed payload's `kind` is supported, exactly one required field
is absent, that field is the kind's observational annotation, no unknown
fields are present, and the completed object passes the full existing
validation (including typed dataclass construction and the non-empty-text
invariants), the decode layer accepts the result and substitutes a
deterministic marker: `<rationale omitted by provider; recovered by
decode>` or `<reason omitted by provider; recovered by decode>`. No
provider call is spent and no repair budget is consumed. The raw malformed
output stays verbatim on the terminal record. The application journals a
`model_repaired` event — `request_id`, `failure_class`
`observational-field`, `result_kind`, `field`, `marker` — causation-linked
to the request, inside the same atomic application batch; it changes no
projection, the same way `model_result_suppressed` does. A suppressed
decision still journals its repair note.

### 3. Rung 2 — compact pinned re-ask (proposed, separately gated)

For an `operational-field` or `invalid-arguments` defect: one bounded
re-ask carrying the request id, the verbatim prior output, the typed
defect, and the instruction to re-emit the complete corrected object with
`action_name` and `arguments` unchanged; returned operational fields must
parse-equal the originals or the repair counts as conflicting and
escalates. The re-ask charges the same decision's reservation and does not
re-send the full context. This rung is deliberately not implemented here:
its cost accounting is meaningful only once provider-reported usage and
context-fit enforcement exist.

### 4. Rung 3 — operator-resumable wait on malformed-output exhaustion (this change)

When a decision's decode defects survive the bounded repair budget, the
attempt does not terminate as a runner failure. The failed decision is
settled exactly as transport failures are, and the session enters the
ADR-0013 durable provider-recovery waiting condition with `failure_class`
`malformed-output`, `operator_action` false, and the bounded detail
'malformed provider output after the repair budget; Resume re-decides,
Stop ends the attempt.' Resume schedules a fresh decide; Stop remains
terminal per ADR-0013; steering and pause work unchanged. This restores,
for the malformed-output class, the retention-for-the-operator outcome that
ADR-0013 clause 3 and clause 6 already demand ('An unclassified failure is
retained for the operator rather than guessed retryable'; 'When automatic
recovery is exhausted, record an explicit provider-recovery waiting
condition'); the implementation had applied those sentences only to
transport classes. Output-exhausted, invalid-request, and unknown classes
keep their existing behavior in this change.

### 5. Negative-signal menu row (ADR-0008)

The decision-repair rate becomes a recorded candidate for the recurring
failure-fingerprint family: repaired decisions and conflicting repairs are
counted from the journal (`model_repaired` plus later rung-2 events),
reported alongside the malformed rate in campaign diagnostics, and
evaluated for inclusion in a shipped supervision policy by the next
supervision experiment. No shipped policy changes in this change; the
ADR-0014 default stays. A drifted provider is visible when `model_repaired`
occurrences cluster, exactly as the run5/run6 malformed streams would have
been had this counter existed.

### 6. What stays unchanged

`rationale` remains required in the prompt and the result contract
(required-with-recovery, not optional). Provider adapters, run settings,
monitor policy defaults, budget accounting, and the validate-then-apply
order for argument contracts are unchanged. Replay must reproduce every
rung-1 acceptance and every rung-3 wait deterministically from the
journal; a live journal containing a `model_repaired` event replays
through a validator branch that changes no projection, matching the
`model_result_suppressed` precedent, and response deduplication identity
extends to include the repair notes so two applications of one decision
cannot diverge on them.

## Implementation and acceptance boundary

Decode and classification in `kernel.py` (typed result; no I/O); repair
journaling and atomic batch commit in `session.py`; retry/wait routing in
`application.py` `_settle_decision_failure`; CLI presentation lines and the
bounded wait detail; replay validator branch in `replay.py`. Workers stay
one bounded call; no benchmark identity selects anything. Tests carry the
actual run6 event-260 payloads as fixtures: rung-1 acceptance with one
provider call, rung-3 waiting after exhaustion with resume re-deciding,
the full-validation gate over operational fields, and replay acceptance of
a journal containing `model_repaired`. The repository's full verification
runs once against the committed change under `baton:verify`.

## Dependents updated in this same change

ARCH-0001 'Failure and recovery behavior' gains the observational/operational
annotation and the observational repair rule, and names malformed-output
exhaustion as an operator-resumable waiting outcome. SCHEM-0001 was reviewed:
it renders no decode or repair path, so no schematic row changes. The index
gains the ADR-0015 row; WORK-0001 records the change and refreshes the
Current handoff. PHASE-0004 status is unchanged: this is runtime hardening
inside the phase's implementation sequence and keep-all-campaigns scope.
