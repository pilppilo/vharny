"""ARC-AGI-3 environment contract and typed models (COD-35).

What the agent sees in an ARC-AGI-3 game and what it may do there, settled
before the runtime adapter (COD-36) and evaluator (COD-37) exist. The kernel
gets the game's exact grid, state and legal actions; nothing here is specific
to one game, and no hint or strategy text reaches a prompt (ADR-0003).

Toolkit interface, verified against the pinned ``arc-agi`` 0.9.10 and
``arcengine`` 0.9.3 (see ``tests/agent/fixtures/arc_agi/README.md``):

* ``arc_agi.Arcade(operation_mode, environments_dir, arc_base_url)``; modes
  ``normal`` (local and API), ``online``, ``offline``, ``competition``. The
  default base URL is ``arcprize.org``; the ARC-AGI-3 API is
  ``https://three.arcprize.org``, so the runtime must pass it.
* ``Arcade.make(game_id, seed=0, ...)`` downloads the game (in ``normal``
  mode) and returns an environment; ``reset()`` and
  ``step(action, data=None, reasoning=None)`` return ``FrameDataRaw | None``.
* ``FrameDataRaw`` fields: ``game_id``, ``state``, ``levels_completed``,
  ``win_levels``, ``action_input``, ``guid``, ``full_reset``,
  ``available_actions``. The grid is the private attribute ``frame``: a list
  of 64x64 numpy arrays that ``model_dump`` leaves out.
* ``GameAction``: ``RESET`` = 0 and ``ACTION1``-``ACTION7``. Only ``ACTION6``
  takes data, ``{"x": 0-63, "y": 0-63}``.
* ``GameState``: ``NOT_PLAYED``, ``NOT_FINISHED``, ``WIN``, ``GAME_OVER``.
* Scorecards: ``open_scorecard()``, ``get_scorecard(id)``,
  ``close_scorecard(id)``; the dump carries an ``api_key`` to drop.

Episode meaning used by this contract:

* **Terminated:** the game reports ``WIN`` or ``GAME_OVER``.
* **Truncated:** the runtime's action limit was reached first. Some games never
  end on their own (``ft09`` ran 400 random clicks without ending), so the
  runtime always runs with a limit.
* **Native goal:** complete the game's levels, as the official scorecard
  judges it. ``ArcGoal`` states the rule; the evaluator applies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, Sequence

from ..errors import ContractError
from ..models import (
    ActionDefinition,
    EnvironmentContract,
    JsonValue,
    RuntimeCapabilities,
    StateRef,
    frozen_mapping,
)

#: Edge length of every ARC-AGI-3 grid, in cells.
GRID_SIZE = 64
#: Largest colour value a cell may hold.
MAX_COLOUR = 15
#: Toolkit id of the reset action.
RESET_ACTION = 0
#: Toolkit ids of the game actions.
GAME_ACTIONS = (1, 2, 3, 4, 5, 6, 7)
#: The one action that takes coordinates.
CLICK_ACTION = 6
#: Largest x or y a click may use.
MAX_COORDINATE = GRID_SIZE - 1

ENVIRONMENT_VERSION = "1"

#: JSON shape of one published frame (``ArcFrame.to_json``).
ARC_OBSERVATION_SCHEMA: Mapping[str, JsonValue] = frozen_mapping(
    {
        "type": "object",
        "properties": {
            "game_id": {"type": "string"},
            "state": {"enum": ("NOT_PLAYED", "NOT_FINISHED", "WIN", "GAME_OVER")},
            "levels_completed": {"type": "integer", "minimum": 0},
            "win_levels": {"type": "integer", "minimum": 0},
            "available_actions": {"type": "array", "items": {"type": "integer"}},
            "full_reset": {"type": "boolean"},
            "grids": {
                "type": "array",
                "description": (
                    "Grid layers, each 64 rows; a row is 64 hex digits, one per "
                    "cell, giving its colour 0-15. Row 0 is the top; x indexes "
                    "columns and y indexes rows."
                ),
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": (
            "game_id",
            "state",
            "levels_completed",
            "win_levels",
            "available_actions",
            "full_reset",
            "grids",
        ),
    }
)


class ArcGameState(str, Enum):
    """Game state as the toolkit reports it."""

    NOT_PLAYED = "NOT_PLAYED"
    NOT_FINISHED = "NOT_FINISHED"
    WIN = "WIN"
    GAME_OVER = "GAME_OVER"

    @property
    def terminal(self) -> bool:
        """True when the game has ended on its own."""
        return self in (ArcGameState.WIN, ArcGameState.GAME_OVER)


Grid = tuple[tuple[int, ...], ...]


class _ToolkitGrid(Protocol):  # pylint: disable=too-few-public-methods
    """A numpy array as the toolkit returns it."""

    def tolist(self) -> list[list[int]]:
        """Return the grid as nested lists."""
        raise NotImplementedError


class _ToolkitState(Protocol):  # pylint: disable=too-few-public-methods
    """``arcengine.GameState``: an enum with a string value."""

    @property
    def value(self) -> str:
        """The state's name, such as ``NOT_FINISHED``."""
        raise NotImplementedError


class ToolkitFrame(Protocol):
    """The parts of ``arcengine.FrameDataRaw`` this module reads (read-only)."""

    @property
    def game_id(self) -> str:
        """Versioned game id, such as ``dc22-fdcac232``."""
        raise NotImplementedError

    @property
    def state(self) -> _ToolkitState:
        """The game state enum."""
        raise NotImplementedError

    @property
    def levels_completed(self) -> int:
        """Levels completed so far."""
        raise NotImplementedError

    @property
    def win_levels(self) -> int:
        """Levels needed to win."""
        raise NotImplementedError

    @property
    def available_actions(self) -> Sequence[int]:
        """Legal action ids for the next step."""
        raise NotImplementedError

    @property
    def full_reset(self) -> bool:
        """Whether the last action reset the whole game."""
        raise NotImplementedError

    @property
    def frame(self) -> Sequence[_ToolkitGrid]:
        """Grid layers as numpy arrays."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ArcFrame:  # pylint: disable=too-many-instance-attributes
    """One observed ARC-AGI-3 frame, validated and JSON-safe."""

    game_id: str
    state: ArcGameState
    levels_completed: int
    win_levels: int
    available_actions: tuple[int, ...]
    full_reset: bool
    grids: tuple[Grid, ...]

    def __post_init__(self) -> None:
        if not self.game_id:
            raise ContractError("ARC frame needs a game_id")
        if not 0 <= self.levels_completed <= max(self.win_levels, 0):
            raise ContractError(
                f"ARC frame reports {self.levels_completed} of "
                f"{self.win_levels} levels completed"
            )
        unknown = sorted(set(self.available_actions) - set(GAME_ACTIONS))
        if unknown:
            raise ContractError(f"ARC frame lists unknown actions {unknown}")
        if len(set(self.available_actions)) != len(self.available_actions):
            raise ContractError("ARC frame lists an action twice")
        for grid in self.grids:
            _check_grid(grid)

    @property
    def terminated(self) -> bool:
        """True when the game reports WIN or GAME_OVER."""
        return self.state.terminal

    @classmethod
    def from_toolkit(cls, frame: ToolkitFrame) -> ArcFrame:
        """Convert a toolkit ``FrameDataRaw`` explicitly, field by field."""
        return cls(
            game_id=str(frame.game_id),
            state=ArcGameState(str(frame.state.value)),
            levels_completed=int(frame.levels_completed),
            win_levels=int(frame.win_levels),
            available_actions=tuple(int(a) for a in frame.available_actions),
            full_reset=bool(frame.full_reset),
            grids=tuple(
                tuple(tuple(int(cell) for cell in row) for row in layer.tolist())
                for layer in frame.frame
            ),
        )

    def to_json(self) -> Mapping[str, JsonValue]:
        """Publish the frame in the ``ARC_OBSERVATION_SCHEMA`` shape."""
        return frozen_mapping(
            {
                "game_id": self.game_id,
                "state": self.state.value,
                "levels_completed": self.levels_completed,
                "win_levels": self.win_levels,
                "available_actions": self.available_actions,
                "full_reset": self.full_reset,
                "grids": tuple(encode_grid(grid) for grid in self.grids),
            }
        )


@dataclass(frozen=True, slots=True)
class ArcAction:
    """One game action: a toolkit action id and, for a click, its cell."""

    action_id: int
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        if self.action_id not in GAME_ACTIONS:
            raise ContractError(
                f"ARC action {self.action_id} is not one of {list(GAME_ACTIONS)}"
            )
        if self.action_id == CLICK_ACTION:
            for name, value in (("x", self.x), ("y", self.y)):
                if value is None:
                    raise ContractError(f"ARC action {CLICK_ACTION} needs {name}")
                if not 0 <= value <= MAX_COORDINATE:
                    raise ContractError(
                        f"ARC action {CLICK_ACTION} {name}={value} is outside "
                        f"0-{MAX_COORDINATE}"
                    )
        elif self.x is not None or self.y is not None:
            raise ContractError(
                f"ARC action {self.action_id} takes no x or y; only "
                f"{CLICK_ACTION} does"
            )

    @classmethod
    def from_arguments(
        cls, arguments: Mapping[str, JsonValue], legal: Sequence[int]
    ) -> ArcAction:
        """Validate a ``step`` action's arguments against the current legal set."""
        unexpected = sorted(set(arguments) - {"action", "x", "y"})
        if unexpected:
            raise ContractError(f"ARC step got unexpected arguments {unexpected}")
        action_id = _integer(arguments.get("action"), "action")
        if action_id not in legal:
            raise ContractError(
                f"ARC action {action_id} is not legal now; legal: {list(legal)}"
            )
        x = arguments.get("x")
        y = arguments.get("y")
        return cls(
            action_id,
            None if x is None else _integer(x, "x"),
            None if y is None else _integer(y, "y"),
        )

    def toolkit_data(self) -> dict[str, int] | None:
        """The ``data`` argument for ``env.step``: coordinates for a click."""
        if self.action_id != CLICK_ACTION:
            return None
        if self.x is None or self.y is None:  # pragma: no cover - __post_init__ checks
            raise ContractError(f"ARC action {CLICK_ACTION} needs x and y")
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class ArcStepOutcome:
    """A frame plus the runtime's action count, with the episode's end state."""

    frame: ArcFrame
    actions_taken: int
    max_actions: int

    def __post_init__(self) -> None:
        if self.max_actions < 1:
            raise ContractError("ARC max_actions must be at least 1")
        if not 0 <= self.actions_taken <= self.max_actions:
            raise ContractError("ARC actions_taken must be within max_actions")

    @property
    def terminated(self) -> bool:
        """The game ended on its own (WIN or GAME_OVER)."""
        return self.frame.terminated

    @property
    def truncated(self) -> bool:
        """The action limit ended the episode before the game did."""
        return not self.terminated and self.actions_taken >= self.max_actions


@dataclass(frozen=True, slots=True)
class ArcGoal:
    """Native goal: complete every level, as the official scorecard judges it.

    The evaluator applies this to the scorecard; the runtime never does.
    """

    description: str = (
        "Complete the game's levels, as the official scorecard judges it."
    )

    def accepts(self, frame: ArcFrame) -> bool:
        """True when the game reports WIN with every level completed."""
        return (
            frame.state is ArcGameState.WIN
            and frame.win_levels > 0
            and frame.levels_completed >= frame.win_levels
        )


def arc_step_schema(legal: Sequence[int]) -> Mapping[str, JsonValue]:
    """Arguments schema for ``step``, generated from the current legal actions.

    The kernel's schema subset has integer bounds but no enum, so the range
    spans the legal ids and ``ArcAction.from_arguments`` rejects the gaps.
    """
    if not legal:
        raise ContractError("ARC frame offers no legal actions")
    properties: dict[str, JsonValue] = {
        "action": {"type": "integer", "minimum": min(legal), "maximum": max(legal)}
    }
    if CLICK_ACTION in legal:
        coordinate: Mapping[str, JsonValue] = {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_COORDINATE,
        }
        properties["x"] = coordinate
        properties["y"] = coordinate
    return frozen_mapping(
        {
            "type": "object",
            "properties": properties,
            "required": ("action",),
            "additionalProperties": False,
        }
    )


def arc_environment_contract(
    frame: ArcFrame, initial_state: StateRef
) -> EnvironmentContract:
    """Declare ``reset`` and ``step`` for the game in ``frame``."""
    legal = frame.available_actions
    step_description = (
        f"Take one game action. Legal action ids now: {', '.join(map(str, legal))}."
    )
    if CLICK_ACTION in legal:
        step_description += (
            f" Action {CLICK_ACTION} acts on one cell and needs x (column) and "
            f"y (row), each 0-{MAX_COORDINATE}."
        )
    return EnvironmentContract(
        environment_id=f"arc-agi-3:{frame.game_id}",
        version=ENVIRONMENT_VERSION,
        initial_state=initial_state,
        actions=(
            ActionDefinition("reset", "Restart the game from its first frame"),
            ActionDefinition("step", step_description, arc_step_schema(legal)),
        ),
        capabilities=RuntimeCapabilities(idempotency=False, reconciliation=False),
    )


def encode_grid(grid: Grid) -> tuple[str, ...]:
    """One hex string per row, one digit per cell."""
    _check_grid(grid)
    return tuple("".join(f"{cell:x}" for cell in row) for row in grid)


def decode_grid(rows: Sequence[str]) -> Grid:
    """Inverse of ``encode_grid``."""
    try:
        grid = tuple(tuple(int(cell, 16) for cell in row) for row in rows)
    except ValueError as exc:
        raise ContractError(f"ARC grid row is not hex: {exc}") from exc
    _check_grid(grid)
    return grid


def _check_grid(grid: Grid) -> None:
    if len(grid) != GRID_SIZE or any(len(row) != GRID_SIZE for row in grid):
        raise ContractError(f"ARC grid must be {GRID_SIZE}x{GRID_SIZE}")
    for row in grid:
        for cell in row:
            if not 0 <= cell <= MAX_COLOUR:
                raise ContractError(f"ARC grid cell {cell} is outside 0-{MAX_COLOUR}")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"ARC {name} must be an integer")
    return value
