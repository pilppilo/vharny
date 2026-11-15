---
id: ADR-0009
title: The operator-built Timing recreation is the PHASE-0004 HTB acceptance target
type: decision
status: accepted
owners: [operator]
created: 2026-10-29
updated: 2026-10-29
depends_on: []
supersedes: []
related: [ADR-0007, ARCH-0001, PHASE-0004, BENCH-0001]
---

# The operator-built Timing recreation is the PHASE-0004 HTB acceptance target

## Context

PHASE-0004 requires "one unscripted codebase acceptance task and one
operator-selected HTB acceptance task, with external evaluation evidence," and
its entrance criteria state that "the operator selects and independently
authorizes any HTB lab before its use." Operator direction 2026-10-25/26
selected a locally deployed, operator-built recreation of retired HTB "Timing"
as the exploitation acceptance target, with no live HTB instance. The
workstream handoff attributed this selection to "the PHASE-0004
operator-selection clause," a clause that does not exist in the phase text,
and no accepted decision recorded the substitution.

## Decision

For PHASE-0004, the operator selects and authorizes the locally deployed,
operator-built recreation of retired HTB "Timing" as the operator-selected HTB
acceptance task, with the user flag as the objective and the root flag as a
stretch milestone. This recorded selection satisfies the phase's
operator-selection and authorization requirements; no live HTB instance is
used. Build material and walkthrough references never enter agent sessions. A
scan cannot substitute for flag capture, and failed trials are retained as
failures. This decision does not amend the phase text; if the operator later
selects a live HTB lab, a successor decision supersedes this one.

## Consequences

- The phase text stands unchanged; the recorded operator selection provides
  the written basis the handoff previously cited as a non-existent clause.
- The grader-validated build chain
  (`coordination/diagnostics/acceptance-20261028/timing-run2-script-build-evidence.md`
  and `validate-20261027-run2.log`, `VALIDATE-PASS` on both flags) and the
  per-run pre-launch gates remain the target-authorization evidence.
- Timing acceptance attempts 1 and 2 stand as recorded failures against this
  ratified target.
- The rung-4 scored-campaign target mode remains governed by the 2026-09-27
  operator record and the rung-4 manifest freeze; this decision covers only
  the PHASE-0004 acceptance task.

## Errata (2026-10-29)

The Context above states that the handoff attributed this selection to "the
PHASE-0004 operator-selection clause," "a clause that does not exist in the
phase text." That characterization is inaccurate. PHASE-0004's entrance
criteria do contain an operator-selection clause — "The operator selects and
independently authorizes any HTB lab before its use," quoted in the Context
above — but that clause governs HTB labs and does not itself authorize a
non-live-HTB substitute.

The Decision above is unchanged: the operator's recorded selection of the
locally deployed, operator-built recreation of retired HTB "Timing" supplies
the written basis the phase text does not. This note is a dated correction
appended to accepted ADR text, not a rewrite of it.

WORK-0001's historical entries (2026-10-29, "the handoff's HTB selection
citation pointed at a phase clause that does not exist") repeat the original
characterization. They are append-only and remain unedited; this errata states
the current account.
