---
id: ADR-0012
title: Recent-band content visibility (renderer, repeat collapsing, derived self-state)
type: decision
status: accepted
owners: [operator]
created: 2026-11-02
updated: 2026-11-02
depends_on: [ARCH-0001]
supersedes: []
related: [ADR-0008, ADR-0011, PHASE-0002, PHASE-0004, WORK-0001]
---

# Recent-band content visibility

## Context

Timing runs 2 and 3 (PHASE-0004 acceptance attempts) share one failure shape:
the model burns its budget on reconnaissance loops and never exploits. The
measured driver is harness-side context visibility. Run 3's 24
`context_assembled` manifests
(`coordination/diagnostics/context-visibility-20261102/run3-baseline.md`)
show the Recent band rendering every journal event as a ~3-token stamp
(`_recent_event_text`, `context.py:697`, renders real content for only two
event kinds), retrieval stuck at 32–39 tokens, 4,931–6,729 manifest tokens
unused per call, and zero `token_budget` omissions — the bands ran out of
content, not budget.

The governed question was framing: proto 04 reserves the EXP process for
"algorithm changes based on untested hypotheses ... rather than quietly
replacing this accepted ranking design." The operator ruled (2026-11-02) that
this is a defect fix: ranking terms, coefficients, MMR penalties, tie-breaks,
and the manifest shape are untouched; the band's documented semantics ("newest
events that fit"; "source excerpts are preferred to summaries when both fit",
ARCH-0001) were not working. The durable choices are recorded here instead of
an EXP pre-registration.

Measured constraints that shape the decision:

- The whitespace-v1 counter undercounts the provider's token count of the
  same request by ~4.1× (call 1) to ~9.5× (call 24) in run 3; provider
  input reached 18,017 tokens at call 24 while the manifest reported ~5.5k
  unused, and call 17 peaked at ~20.4k provider tokens (in+out). Naive
  fill-to-budget against the harness's whitespace headroom would exceed the
  real model window.
- stdout lives in the artifact store; receipts carry `raw_artifact` refs and
  no inline output. `context.py` performs no I/O (proto 04), so excerpt
  content must be pre-resolved by the session and passed in as data.
- Runtime runs journal no `evidence_fingerprints` (measured 24/24 in run 3):
  the application coordinator's runtime dispatch path (`application.py:653`)
  bypasses `_dispatch_action`, leaving `Session._action_names` empty, so the
  gate at `session.py:2175` filters out every receipt. The operator-selected
  journal-at-receipt-time evidence mapping (2026-11-01 05:20 UTC entry,
  ADR-0011 era) is therefore silently inert in runtime runs. This is a
  supervision-side defect with a separate fix; this decision does not depend
  on it.

## Decision

1. **Framing:** defect fix under ARCH-0001's documented band semantics.
   Ranking terms, coefficients, duplicate penalties, stable tie-breaks, and
   the manifest shape are unchanged. No EXP pre-registration.
2. **Per-kind renderer:** `_recent_event_text` renders bounded content for
   journaled event kinds — `receipt_received` (action name, bounded command
   summary, status, bounded stdout excerpt, artifact-digest source link),
   `action_intended`, `model_decided`, attempt events, guidance and operator
   events, knowledge and investigation records — with per-item token caps
   and a bare-prefix fallback for unmapped kinds. Every derived line links
   its source span.
3. **Bounded receipt-content map:** the session pre-resolves stdout excerpts
   and the ADR-0008 normalized digest (`stdout_fingerprints`, reused as a
   pure function) for the most recent successful evidence-bearing receipts
   and passes them to `assemble_context` as data. `context.py` stays
   I/O-free and deterministic; the map is part of the assembly input, and
   artifact reads stay journaled as today.
4. **Repeat collapsing:** a receipt whose normalized digest matches an
   already-rendered receipt renders once as a counted line ("identical
   output to event M, seen K× since") instead of N near-identical excerpts,
   keyed on the digest supplied in the content map; omissions stay typed;
   determinism follows from the existing newest-first walk.
5. **Recent-band cap:** a recorded module constant caps the Recent band's
   token share (default 1000 whitespace tokens) because the harness counter
   undercounts provider tokens as measured above. Raising it or making it a
   run setting happens through a run predeclaration, never silently.
6. **Derived self-state block:** a bounded mandatory-band item built from
   the existing memory projections — open investigations, knowledge item
   statuses, active attempt, committed-lineage head — every line
   source-linked, state only (no directive language). ARCH-0001's Mandatory
   enumeration is amended in this same change to carry it.
7. **Policy version:** `POLICY_VERSION` 1 → 2 so manifests stay auditable
   across the switch.
8. **Validation:** offline seeded assertions (band composition, excerpt
   rendering, collapse determinism, typed overflow, manifest policy
   version) beside the existing context tests; the live burn effect is
   observed at the already-planned Timing attempt-4 acceptance run under a
   fresh operator predeclaration, with provider usage among the predeclared
   criteria.

## Consequences

- The model sees its own established work: the measured ~2% own-history
  share becomes content-bearing within the recorded cap, and repeated probe
  output collapses instead of restating.
- The whitespace counter's provider gap remains open; the recorded cap
  bounds its risk. A faithful counter or provider-usage feedback is a
  future governed change, not a silent one.
- Runtime runs still journal no `evidence_fingerprints` until the
  supervision-side defect is fixed; the monitor's B2 signal stays degraded
  in runtime runs until then (separate fix, same blindness family).
- Benchmark identity plays no role in any of this; supervision policy is
  untouched; `context.py` responsibilities stay in `context.py`.
