"""Label board-game moves by what they did: forward, return or weak (ADR-0023).

An ARC-AGI-3 observation is a grid of equal-length rows. Exact board equality
cannot recognize a revisited position, because a counter or timer may change a
few cells on every move. Masking cells that always change does not work
either, because such a counter moves. What separates the cases is distance,
the number of differing cells, measured against the largest move seen on the
level:

- a **weak** move changed less than a quarter of the largest move;
- a **return** changed the board at least that much, and the new board is
  within a quarter of the largest move of a board from two or more moves back;
- a **forward** move is neither.

The first move on a level has no board before it here, so it is labelled
``first`` and is never counted as a repeat. A level ends when
``levels_completed`` changes, the game reports a full reset, or the board
changes shape.

This module is pure: callers extract the steps from the journal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .models import JsonValue

#: A return or weak move is within 1/RETURN_DIVISOR of the level's largest move.
RETURN_DIVISOR = 4


class MoveLabel(str, Enum):
    """What one move did to the board."""

    FIRST = "first"
    FORWARD = "forward"
    RETURN = "return"
    WEAK = "weak"


@dataclass(frozen=True, slots=True)
class BoardStep:
    """One successful move: its arguments and the board it produced."""

    sequence: int
    arguments: Mapping[str, JsonValue]
    board: tuple[str, ...]
    levels_completed: int | None
    full_reset: bool
    legal_actions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LabeledMove:
    """A move with its label and the cells it changed (None for the first)."""

    step: BoardStep
    label: MoveLabel
    change: int | None


@dataclass(frozen=True, slots=True)
class BoardLevel:
    """The labelled moves of the current level and its untried legal actions."""

    moves: tuple[LabeledMove, ...]
    untried_actions: tuple[int, ...]


def board_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int | None:
    """Return how many cells differ, or None when the boards differ in shape."""
    if len(left) != len(right) or any(
        len(a) != len(b) for a, b in zip(left, right, strict=True)
    ):
        return None
    return sum(
        a != b
        for row_left, row_right in zip(left, right, strict=True)
        for a, b in zip(row_left, row_right, strict=True)
    )


def current_level(steps: Sequence[BoardStep]) -> BoardLevel | None:
    """Label the moves of the latest level; None when there are no steps."""
    if not steps:
        return None
    start = 0
    for index in range(1, len(steps)):
        previous, step = steps[index - 1], steps[index]
        if (
            step.full_reset
            or step.levels_completed != previous.levels_completed
            or board_distance(previous.board, step.board) is None
        ):
            start = index
    level = steps[start:]
    tried = {
        action
        for step in level
        if isinstance(action := step.arguments.get("action"), int)
        and not isinstance(action, bool)
    }
    untried = tuple(a for a in level[-1].legal_actions if a not in tried)
    return BoardLevel(_label(level), untried)


def _label(level: Sequence[BoardStep]) -> tuple[LabeledMove, ...]:
    changes: list[int] = []
    for previous, step in zip(level[:-1], level[1:]):
        distance = board_distance(previous.board, step.board)
        changes.append(0 if distance is None else distance)
    largest = max(changes, default=0)
    moves = [LabeledMove(level[0], MoveLabel.FIRST, None)]
    for index, change in enumerate(changes, start=1):
        moves.append(
            LabeledMove(
                level[index], _move_label(level, index, change, largest), change
            )
        )
    return tuple(moves)


def _move_label(
    level: Sequence[BoardStep], index: int, change: int, largest: int
) -> MoveLabel:
    if largest == 0 or change * RETURN_DIVISOR < largest:
        return MoveLabel.WEAK
    board = level[index].board
    for earlier in level[: index - 1]:
        distance = board_distance(earlier.board, board)
        if distance is not None and distance * RETURN_DIVISOR <= largest:
            return MoveLabel.RETURN
    return MoveLabel.FORWARD
