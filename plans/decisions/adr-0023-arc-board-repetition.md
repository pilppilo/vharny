---
id: ADR-0023
title: Detect repetition on a board from what each move did
type: decision
status: accepted
owners: [operator]
created: 2026-11-18
updated: 2026-11-18
depends_on: [ADR-0008, ADR-0013, ADR-0014]
supersedes: []
related: [ARCH-0001, PHASE-0004, BENCH-0001]
---

# Detect repetition on a board from what each move did

## Context and decision requested

`monitor.p6.v1`, the default that ADR-0014 adopted, counts a repeat whenever
the same normalized action shows up three times in its window, whatever the
action did. The `ls20` re-run
(`evidence/phase-0004-longcat-trial/rerun-ls20/`) showed what that does on a
board game, by comparing the saved grids:

| Moves | Action | What happened |
| --- | --- | --- |
| 1–3 | `1` | Three different positions. Each move changed 52 cells, with the changed region 5 rows higher each time. |
| 4–9 | `2` and `1`, alternating | Each move returned to the position after move 2 or move 3: 4 cells from it, against 52 for a move. |
| 10 | `3`, its first use | Only 2 counter cells changed, so the move had no effect. |

Legal actions were `1` to `4` throughout, and action `4` was never tried.

So p6 got both sides wrong:

- **It fired on progress.** The three forward moves after move 3 triggered it.
- **It never saw the real loop.** Observation fingerprints can't see it,
  because a counter in rows 61–62 changes cells on every move.

Two simple fixes fail:

- **Exact board equality** misses the loop, because of the counter.
- **Masking the cells that change on every move** also misses it: no single
  cell changed on every move, because the counter moves.

The operator drafted this decision with a first implementation on 2026-11-18,
and approved the revision below the same day (Linear COD-93).

## Decision

### 1. Label each move by what it did

On the current level, each successful `step` is labelled by its distance (the
number of differing cells), measured against the largest move seen on the
level:

| Label | Rule |
| --- | --- |
| weak | It changed less than a quarter of the largest move. |
| return | It changed at least that much, and the new board is within a quarter of the largest move of a board two or more moves back. |
| forward | Neither. |
| first | The level's first move. It has no earlier board here, so it never counts as a repeat. |

A level ends when `levels_completed` changes, the game reports a full reset,
or the board changes shape. The labels live in `board_moves.py`, which is pure.
The supervisor supplies the observations from successful `arc-agi-3-result`
receipts and caches them by content digest, so a run does not re-read them
on every decision.

### 2. `monitor.p7.v1` counts repeats from the labels

`monitor.p7.v1` keeps p6's numbers: window 8, threshold 3, cooldown 4, the
evidence fingerprints and the supported-progress checkpoint. Only what counts
as a repeat of a `step` changes:

- a forward or first move counts as no repeat;
- a weak move counts as a repeat of its action, as under p6, so pressing
  against a wall is still caught;
- every return counts toward one shared fingerprint, which names the legal
  actions not yet tried on the level, so the supervisor's guidance can point
  at them.

Returns are found against every earlier board on the level, so a loop of any
length is caught, not only a back-and-forth.

### 3. Only board games use it

p6 stays the default. The application selects p7 only for ARC-AGI-3 runs, and
only p7 reads observations. Every other run, and every run created before this
change, keeps p6 exactly. Runs already bound to p6 therefore still reopen,
since reopening checks the recorded policy.

## Consequences

- **The divisor of 4 is a first value.** It comes from one game's distances.
  Before scored runs, check it on the other development games, including a
  game whose real moves change only a few cells.
- **A level where no move has done anything is not caught here.** If every
  move so far changed only the counter, no move is large enough to measure
  against. The budget checkpoint still covers that case.
- **The "no progress" trigger still reads every new board as progress,**
  because of the counter. Board-aware evidence fingerprints would be a
  separate change.
- **Results under p6 and p7 are not comparable** and must not be pooled.

## Dependents updated in the same change

- ARCH-0001's signals paragraph.
- The index row.
- WORK-0001.

ADR-0014 is not amended: its default is unchanged.

## Alternatives considered

- **Exact board equality, or masking the cells that always change.** Both
  miss the `ls20` loop (Context).
- **Comparing only with the board two moves back.** This was the first draft.
  It misses longer loops.
- **Making p7 the default for every environment.** Rejected. It would stop
  p6-bound runs from reopening, and would record a version change for runs
  whose behavior did not change.
- **Dropping action repeats for every `step` on a board.** This was the first
  draft. It stops detecting a repeated move that does nothing.
