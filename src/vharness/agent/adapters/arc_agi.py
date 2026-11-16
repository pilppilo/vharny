"""ARC-AGI-3 environment contract and typed models (COD-35, hardened in COD-70).

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
  ``win_levels``, ``action_input`` (``id``: a ``GameAction`` whose ``value``
  is the int id, ``data``, ``reasoning``), ``guid``, ``full_reset``,
  ``available_actions``. The grid is the private attribute ``frame``: a list
  of 64x64 numpy arrays (animation layers) that ``model_dump`` leaves out.
* ``GameAction``: ``RESET`` = 0 and ``ACTION1``-``ACTION7``. Only ``ACTION6``
  takes data, ``{"x": 0-63, "y": 0-63}``.
* ``GameState``: ``NOT_PLAYED``, ``NOT_FINISHED``, ``WIN``, ``GAME_OVER``.
* Scorecards: ``open_scorecard()``, ``get_scorecard(id)``,
  ``close_scorecard(id)``; the dump carries an ``api_key`` to drop.

Design:

* **One fixed contract.** The session pins its environment contract at
  creation and refuses a resume whose contract differs, so the contract never
  depends on a frame: ``step`` always takes ids 1-7 and optional ``x``/``y``.
  Legality is checked per frame at the boundary (``ArcAction.from_arguments``).
* **Observation vs evidence.** A frame may hold many animation layers. The
  observation the model sees (``ArcFrame.to_json``) carries only the final
  layer and a layer count; ``evidence_json`` keeps every layer, the run
  ``guid`` and the ``action_input`` echo for storage.
* **Strict boundary.** No coercion: a field of the wrong type is refused, and
  every toolkit conversion failure is a ``ContractError``.

Episode meaning:

* **Terminated:** the game reports ``WIN`` or ``GAME_OVER``; a terminated frame
  offers no legal action, and only ``reset`` is possible.
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

ENVIRONMENT_VERSION = "2"

STEP_DESCRIPTION = (
    "Take one game action, id 1-7. The current observation lists the legal "
    "ids; any other id is refused. Action 6 acts on one cell and needs x "
    "(column) and y (row), each 0-63; other actions take no x or y. When the "
    "game is over, only reset is possible."
)
RESET_DESCRIPTION = "Restart the game from its first frame"

_COORDINATE: Mapping[str, JsonValue] = {
    "type": "integer",
    "minimum": 0,
    "maximum": MAX_COORDINATE,
}

#: Arguments schema for ``step``; the same for every game and every frame.
ARC_STEP_SCHEMA: Mapping[str, JsonValue] = frozen_mapping(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "integer",
                "minimum": min(GAME_ACTIONS),
                "maximum": max(GAME_ACTIONS),
            },
            "x": _COORDINATE,
            "y": _COORDINATE,
        },
        "required": ("action",),
        "additionalProperties": False,
    }
)

#: JSON shape of the observation the model sees (``ArcFrame.to_json``).
ARC_OBSERVATION_SCHEMA: Mapping[str, JsonValue] = frozen_mapping(
    {
        "type": "object",
        "properties": {
            "game_id": {"type": "string"},
            "state": {"enum": ("NOT_PLAYED", "NOT_FINISHED", "WIN", "GAME_OVER")},
            "levels_completed": {"type": "integer", "minimum": 0},
            "win_levels": {"type": "integer", "minimum": 0},
            "legal_actions": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Empty once the game is over.",
            },
            "full_reset": {"type": "boolean"},
            "grid": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The final grid layer: 64 rows, each 64 hex digits, one "
                    "per cell, giving its colour 0-15. Row 0 is the top; x "
                    "indexes columns and y indexes rows."
                ),
            },
            "layer_count": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Animation layers in this frame; only the last is shown."
                ),
            },
        },
        "required": (
            "game_id",
            "state",
            "levels_completed",
            "win_levels",
            "legal_actions",
            "full_reset",
            "grid",
            "layer_count",
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


class _ToolkitEnum(Protocol):  # pylint: disable=too-few-public-methods
    """An ``arcengine`` enum member: ``GameState`` or ``GameAction``."""

    @property
    def value(self) -> object:
        """The member's value: a state name or an action id."""
        raise NotImplementedError


class _ToolkitActionInput(Protocol):
    """``arcengine.ActionInput``: the action a frame answers."""

    @property
    def id(self) -> _ToolkitEnum:
        """The ``GameAction`` sent."""
        raise NotImplementedError

    @property
    def data(self) -> Mapping[str, object]:
        """Its data, ``{"x": ..., "y": ...}`` for a click."""
        raise NotImplementedError


class ToolkitFrame(Protocol):
    """The parts of ``arcengine.FrameDataRaw`` this module reads (read-only)."""

    @property
    def game_id(self) -> object:
        """Versioned game id, such as ``dc22-fdcac232``."""
        raise NotImplementedError

    @property
    def state(self) -> _ToolkitEnum:
        """The ``GameState`` member."""
        raise NotImplementedError

    @property
    def levels_completed(self) -> object:
        """Levels completed so far."""
        raise NotImplementedError

    @property
    def win_levels(self) -> object:
        """Levels needed to win."""
        raise NotImplementedError

    @property
    def available_actions(self) -> Sequence[object]:
        """Action ids the game lists for the next step."""
        raise NotImplementedError

    @property
    def full_reset(self) -> object:
        """Whether the last action reset the whole game."""
        raise NotImplementedError

    @property
    def guid(self) -> object:
        """The run id the scorecard uses, or ``None``."""
        raise NotImplementedError

    @property
    def action_input(self) -> _ToolkitActionInput | None:
        """Echo of the action this frame answers."""
        raise NotImplementedError

    @property
    def frame(self) -> Sequence[_ToolkitGrid]:
        """Grid layers as numpy arrays."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ArcActionEcho:
    """The action a frame says it answers: an id 0-7 and, for a click, a cell."""

    action_id: int
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.action_id, "action_input id", 0, max(GAME_ACTIONS))
        if (self.x is None) != (self.y is None):
            raise ContractError("ARC action_input needs both x and y, or neither")
        if self.x is not None and self.y is not None:
            _strict_int(self.x, "action_input x", 0, MAX_COORDINATE)
            _strict_int(self.y, "action_input y", 0, MAX_COORDINATE)

    def to_json(self) -> Mapping[str, JsonValue]:
        """JSON form for evidence."""
        return frozen_mapping({"action": self.action_id, "x": self.x, "y": self.y})


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
    guid: str | None = None
    action_input: ArcActionEcho | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, str) or not self.game_id:
            raise ContractError("ARC frame needs a non-empty game_id string")
        if not isinstance(self.state, ArcGameState):
            raise ContractError("ARC frame state must be an ArcGameState")
        _strict_int(self.win_levels, "win_levels", 0)
        _strict_int(self.levels_completed, "levels_completed", 0, self.win_levels)
        if not isinstance(self.full_reset, bool):
            raise ContractError("ARC frame full_reset must be a boolean")
        if not isinstance(self.available_actions, tuple):
            raise ContractError("ARC frame available_actions must be a tuple")
        for action in self.available_actions:
            _strict_int(
                action, "available action", min(GAME_ACTIONS), max(GAME_ACTIONS)
            )
        if len(set(self.available_actions)) != len(self.available_actions):
            raise ContractError("ARC frame lists an action twice")
        if not self.grids:
            raise ContractError("ARC frame has no grid layers")
        for grid in self.grids:
            _check_grid(grid)
        if self.guid is not None and (not isinstance(self.guid, str) or not self.guid):
            raise ContractError("ARC frame guid must be a non-empty string or None")
        if self.action_input is not None and not isinstance(
            self.action_input, ArcActionEcho
        ):
            raise ContractError("ARC frame action_input must be an ArcActionEcho")

    @property
    def terminated(self) -> bool:
        """True when the game reports WIN or GAME_OVER."""
        return self.state.terminal

    @property
    def legal_actions(self) -> tuple[int, ...]:
        """Actions ``step`` may take now: none once the game is over."""
        return () if self.terminated else self.available_actions

    @property
    def final_grid(self) -> Grid:
        """The last animation layer: what the game shows after this frame."""
        return self.grids[-1]

    @classmethod
    def from_toolkit(cls, frame: ToolkitFrame) -> ArcFrame:
        """Convert a toolkit ``FrameDataRaw`` field by field, without coercion.

        Any malformed field, missing attribute or unknown value raises
        ``ContractError`` naming the problem.
        """
        try:
            guid = frame.guid
            return cls(
                game_id=_strict_str(frame.game_id, "game_id"),
                state=ArcGameState(_strict_str(frame.state.value, "state")),
                levels_completed=_strict_int(
                    frame.levels_completed, "levels_completed"
                ),
                win_levels=_strict_int(frame.win_levels, "win_levels"),
                available_actions=tuple(
                    _strict_int(action, "available action")
                    for action in frame.available_actions
                ),
                full_reset=_strict_bool(frame.full_reset, "full_reset"),
                grids=tuple(_toolkit_grid(layer.tolist()) for layer in frame.frame),
                guid=None if guid is None else _strict_str(guid, "guid"),
                action_input=_toolkit_echo(frame.action_input),
            )
        except ContractError as exc:
            raise ContractError(f"ARC toolkit frame is malformed: {exc}") from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise ContractError(
                f"ARC toolkit frame is malformed: {type(exc).__name__}: {exc}"
            ) from exc

    def to_json(self) -> Mapping[str, JsonValue]:
        """The observation the model sees, in the ``ARC_OBSERVATION_SCHEMA`` shape."""
        return frozen_mapping(
            {
                "game_id": self.game_id,
                "state": self.state.value,
                "levels_completed": self.levels_completed,
                "win_levels": self.win_levels,
                "legal_actions": self.legal_actions,
                "full_reset": self.full_reset,
                "grid": encode_grid(self.final_grid),
                "layer_count": len(self.grids),
            }
        )

    def evidence_json(self) -> Mapping[str, JsonValue]:
        """Everything the frame holds, for stored evidence: every layer, guid, echo."""
        return frozen_mapping(
            {
                "game_id": self.game_id,
                "state": self.state.value,
                "levels_completed": self.levels_completed,
                "win_levels": self.win_levels,
                "available_actions": self.available_actions,
                "full_reset": self.full_reset,
                "grids": tuple(encode_grid(grid) for grid in self.grids),
                "guid": self.guid,
                "action_input": (
                    None if self.action_input is None else self.action_input.to_json()
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ArcAction:
    """One game action: a toolkit action id and, for a click, its cell."""

    action_id: int
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.action_id, "action", min(GAME_ACTIONS), max(GAME_ACTIONS))
        if self.action_id == CLICK_ACTION:
            for name, value in (("x", self.x), ("y", self.y)):
                if value is None:
                    raise ContractError(f"ARC action {CLICK_ACTION} needs {name}")
                _strict_int(value, f"action {CLICK_ACTION} {name}", 0, MAX_COORDINATE)
        elif self.x is not None or self.y is not None:
            raise ContractError(
                f"ARC action {self.action_id} takes no x or y; only "
                f"{CLICK_ACTION} does"
            )

    @classmethod
    def from_arguments(
        cls, arguments: Mapping[str, JsonValue], frame: ArcFrame
    ) -> ArcAction:
        """Validate a ``step`` action's arguments against the current frame."""
        if frame.terminated:
            raise ContractError(
                f"the game is over ({frame.state.value}); only reset is possible"
            )
        unexpected = sorted(set(arguments) - {"action", "x", "y"})
        if unexpected:
            raise ContractError(f"ARC step got unexpected arguments {unexpected}")
        action_id = _strict_int(arguments.get("action"), "action")
        legal = frame.legal_actions
        if action_id not in legal:
            raise ContractError(
                f"ARC action {action_id} is not legal now; legal: {list(legal)}"
            )
        x = arguments.get("x")
        y = arguments.get("y")
        return cls(
            action_id,
            None if x is None else _strict_int(x, "x"),
            None if y is None else _strict_int(y, "y"),
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
        _strict_int(self.max_actions, "max_actions", 1)
        _strict_int(self.actions_taken, "actions_taken", 0, self.max_actions)

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


def arc_environment_contract(
    game_id: str, initial_state: StateRef
) -> EnvironmentContract:
    """Declare ``reset`` and ``step``; the same for every frame of a game."""
    if not isinstance(game_id, str) or not game_id:
        raise ContractError("ARC contract needs a non-empty game_id string")
    return EnvironmentContract(
        environment_id=f"arc-agi-3:{game_id}",
        version=ENVIRONMENT_VERSION,
        initial_state=initial_state,
        actions=(
            ActionDefinition("reset", RESET_DESCRIPTION),
            ActionDefinition("step", STEP_DESCRIPTION, ARC_STEP_SCHEMA),
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
    except (TypeError, ValueError) as exc:
        raise ContractError(f"ARC grid row is not hex: {exc}") from exc
    _check_grid(grid)
    return grid


def _toolkit_grid(rows: object) -> Grid:
    if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
        raise ContractError("ARC grid layer is not a list of rows")
    return tuple(
        tuple(_strict_int(cell, "grid cell", 0, MAX_COLOUR) for cell in row)
        for row in rows
    )


def _toolkit_echo(action_input: _ToolkitActionInput | None) -> ArcActionEcho | None:
    if action_input is None:
        return None
    data = action_input.data
    x = data.get("x")
    y = data.get("y")
    return ArcActionEcho(
        _strict_int(action_input.id.value, "action_input id"),
        None if x is None else _strict_int(x, "action_input x"),
        None if y is None else _strict_int(y, "action_input y"),
    )


def _check_grid(grid: object) -> None:
    if (
        not isinstance(grid, tuple)
        or len(grid) != GRID_SIZE
        or any(not isinstance(row, tuple) or len(row) != GRID_SIZE for row in grid)
    ):
        raise ContractError(f"ARC grid must be {GRID_SIZE}x{GRID_SIZE}")
    for row in grid:
        for cell in row:
            _strict_int(cell, "grid cell", 0, MAX_COLOUR)


def _strict_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"ARC {name} must be a non-empty string, not {value!r}")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"ARC {name} must be a boolean, not {value!r}")
    return value


def _strict_int(
    value: object, name: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Return ``value`` if it is an int (not a bool) within the bounds."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"ARC {name} must be an integer, not {value!r}")
    if minimum is not None and value < minimum:
        raise ContractError(f"ARC {name} {value} is below {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"ARC {name} {value} is above {maximum}")
    return value
