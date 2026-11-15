---
id: ADR-0010
title: Verification-gated completion and oracle provenance
type: decision
status: accepted
owners: [operator]
created: 2026-10-31
updated: 2026-10-31
depends_on: []
supersedes: []
related: [ADR-0008, PHASE-0004, EXP-0002, BENCH-0001]
---

# Verification-gated completion and oracle provenance

## Context

Two gaps in the 2026-10-30/31 record show governed completion and evidence
being asserted beyond what had been verified when the record was written.

1. EXP-0002 was flipped to `completed` (2026-10-30 21:20 UTC, WORK-0001) with
   only narrow checks run; the full verification gate ran hours later and was
   red, and the failure sat unrecorded for roughly 11.5 hours.
2. The Timing attempt-2 review's normalized evidence fingerprints were
   computed with a session-local rule that was never committed, so only 5 of
   30 rows could match on replay. The EXP-0002 completion record carries this
   as its one replay deviation ("the review's non-empty normalization was
   never committed"), with the fire table unaffected because the R1
   fingerprints matched on 30/30 rows. Source records: WORK-0001's
   2026-10-30 21:20 UTC entry;
   `coordination/diagnostics/acceptance-20261028/timing-accept-attempt-2-review.md`
   and `review-data/`.

## Decision

For future governed work:

1. **Verification-gated completion.** A governed status flip to `completed`
   — for an experiment or a phase — requires a green full verification gate
   at the committed revision being completed, or the flip must explicitly
   record the full gate as still pending. Narrow test runs do not substitute
   for the gate.
2. **Oracle provenance.** A committed oracle or CSV presented as evidence is
   not reproducible evidence unless it carries its exact computation recipe
   in the repository — a committed script or a documented derivation. A
   computation that lives only in a session cannot be replayed and cannot
   anchor a cross-check claim.

## Consequences

- These rules apply to future governed status flips and to future evidence
  bundles. They do not retroactively alter completed records: EXP-0002's
  record stands as written, and its R2 deviation note already documents the
  fingerprint issue.
- A flip that records the full gate as still pending is a permitted form, but
  the eventual gate outcome then belongs in the workstream record instead of
  sitting unrecorded; the roughly 11.5-hour gap above is the failure this
  rule closes.
