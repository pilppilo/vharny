---
id: ADR-0018
title: Apply the planning protocol by hand; the Plant CLI is retired
type: decision
status: accepted
owners: [operator]
created: 2026-11-16
updated: 2026-11-16
depends_on: []
supersedes: [ADR-0006]
related: [WORK-0001, PHASE-0004]
---

# Apply the planning protocol by hand; the Plant CLI is retired

## Context

ADR-0006 made the Plant Required packet, printed by `python tools/plant.py
context plans`, the source for mutable planning direction. This repository
does not contain `tools/plant.py`. `plans/.plant-lock.json` pinned a hash for a
CLI at `../tools/plant.py` that is absent, so neither the `context` nor the
`check` command can run, and the lock cannot be verified.

The operator decided on 2026-11-16: "we wont use plant hence forward so just
obeying protocol will be our standard".

## Decision

The Plant CLI is not used in this repository. `plans/PROTOCOL.md` remains the
planning standard and is applied by reading it and reviewing changes against
it.

For mutable planning direction, including current phase status, readiness,
scope, blockers, and next action, start from the Current focus block in
`plans/README.md`. The initial context is the same content the packet used to
carry, read directly:

1. Current focus in `plans/README.md`.
2. The `Current handoff (non-authoritative)` section of the workstream that
   Current focus names.
3. Each heading listed as Required in the Read manifest, in its governing
   document.

Now and Read IDs remain authoritative references and are expanded when that
context is incomplete or ambiguous, a conflict appears, or the task changes
architecture, verifies phase status, or edits plans. Where the protocol says
"the Required packet emitted by the context command", read those three items.
Read manifest entries name a document ID and an exact heading; hash-pinned
line ranges are not used.

The checks the CLI's `check` command performed are done by review. A planning
change is reviewed against PROTOCOL §3 (index contents and Current focus), §5
(frontmatter), §8 (dependencies), §12 (change protocol), and §13 (quality
gate) before it is merged.

These rules from ADR-0006 continue unchanged. The Current handoff is a
selector and summary, never a second requirements source. Repository
instructions hold stable engineering boundaries and must not duplicate mutable
planning status. A conflict between governing documents is resolved only when
an accepted ADR resolves that same conflict; otherwise follow PROTOCOL §2 and
stop for operator resolution.

`plans/.plant-lock.json` is removed. `PROTOCOL.md`, `schema/`, and `templates/`
stay as ordinary repository files taken from Plant source revision
`c4f886e71f96b0b15a226be241155a8573579e01`. They change only in an explicit,
reviewed planning change.

## Consequences

No planning document instructs a reader to run a tool that is absent from the
repository. Agreement between the index, frontmatter, and document statuses is
no longer machine-checked, so a planning change that touches a status must
update its index row in the same change, as §12 already requires, and the
reviewer confirms it.

Earlier workstream entries and phase text that mention the CLI, the lock, or
packet hashes are historical records and are not edited.

## Alternatives considered

- Restore the Plant CLI from its source project. Rejected by the operator.
- Keep ADR-0006 and treat the packet instruction as inapplicable. Rejected:
  an accepted decision would keep naming a source of direction that cannot be
  produced.

## Evidence and references

- Operator decision, 2026-11-16, quoted above.
- `tools/plant.py` is absent at `9951274`; `plans/.plant-lock.json` recorded
  `installed_cli.path` as `../tools/plant.py`.
- ADR-0006, superseded by this decision.
