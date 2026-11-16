"""Toolkit I/O for ARC-AGI-3: one game, driven through the official package.

Everything that touches the ``arc-agi`` toolkit or a worker thread lives here,
so the runtime (``arc_agi_runtime``) and the contract (``arc_agi``) stay free
of external I/O.

Calls run on a daemon worker thread with an explicit timeout, because the
toolkit's own calls take none. A call that times out keeps running in the
background; until it finishes, the caller refuses new calls rather than
overlap them on the same game.

The API key is never handled here. The toolkit reads ``ARC_API_KEY`` from the
environment itself; this module neither reads nor stores its value.
"""

from __future__ import annotations

import importlib.metadata
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TypeVar

from ..errors import AgentError, ContractError
from ..models import JsonValue, frozen_mapping
from .arc_agi import ToolkitFrame

#: The ARC-AGI-3 API. Toolkit 0.9.10 defaults to arcprize.org, which is wrong.
ARC_API_BASE_URL = "https://three.arcprize.org"
#: Environment variable the toolkit reads the API key from. Named, never read.
ARC_API_KEY_ENV = "ARC_API_KEY"
#: Default bound on any one toolkit call, in seconds.
DEFAULT_CALL_TIMEOUT_SECONDS = 30.0

_T = TypeVar("_T")


class ArcCallError(AgentError):
    """A toolkit call failed or timed out; whether it took effect is unknown."""


class ArcCallBusy(AgentError):
    """An earlier timed-out call is still running; nothing new was sent."""


@dataclass(frozen=True, slots=True)
class ArcToolkitInfo:
    """What a game connection runs on. Pinned in the runtime identity."""

    toolkit_version: str
    engine_version: str
    game_id: str
    seed: int
    operation_mode: str
    base_url: str

    def __post_init__(self) -> None:
        for name in (
            "toolkit_version",
            "engine_version",
            "game_id",
            "operation_mode",
            "base_url",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractError(f"ARC toolkit info needs a non-empty {name}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ContractError("ARC toolkit info seed must be an integer")

    def to_json(self) -> Mapping[str, JsonValue]:
        """JSON form for the identity digest."""
        return frozen_mapping(
            {
                "toolkit_version": self.toolkit_version,
                "engine_version": self.engine_version,
                "game_id": self.game_id,
                "seed": self.seed,
                "operation_mode": self.operation_mode,
                "base_url": self.base_url,
            }
        )


class ArcGameConnection(Protocol):
    """One open toolkit game. Calls may block; the runtime bounds them."""

    @property
    def info(self) -> ArcToolkitInfo:
        """What this connection runs on."""
        raise NotImplementedError

    def reset(self) -> ToolkitFrame | None:
        """Restart the game; the toolkit may answer ``None``."""
        raise NotImplementedError

    def step(
        self, action_id: int, data: Mapping[str, int] | None
    ) -> ToolkitFrame | None:
        """Send one game action; the toolkit may answer ``None``."""
        raise NotImplementedError

    def close(self) -> None:
        """Release local resources. Never closes the scorecard."""
        raise NotImplementedError


class TimedCaller:
    """Run blocking calls on a daemon thread with a timeout, one at a time."""

    def __init__(self, timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ContractError("ARC call timeout must be finite and positive")
        self.timeout_seconds = float(timeout_seconds)
        self._running: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        """An earlier call timed out and is still running."""
        return self._running is not None and self._running.is_alive()

    def call(self, what: str, function: Callable[[], _T]) -> _T:
        """Return ``function()`` or raise ``ArcCallError`` with the cause kept."""
        if self.busy:
            raise ArcCallBusy(
                f"an earlier ARC toolkit call is still running; {what} was not sent"
            )
        result: list[_T] = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                result.append(function())
            except BaseException as exc:  # pylint: disable=broad-exception-caught
                # Re-raised below on the calling thread, with its cause.
                failure.append(exc)

        worker = threading.Thread(target=run, name=f"arc-toolkit {what}", daemon=True)
        self._running = worker
        worker.start()
        worker.join(self.timeout_seconds)
        if worker.is_alive():
            raise ArcCallError(
                f"{what} did not answer within {self.timeout_seconds:g}s; "
                "whether it took effect is unknown"
            )
        self._running = None
        if failure:
            exc = failure[0]
            raise ArcCallError(
                f"{what} raised {type(exc).__name__}: {exc}; "
                "whether it took effect is unknown"
            ) from exc
        return result[0]


class ToolkitGameConnection:
    """An ``ArcGameConnection`` over the official ``arc-agi`` package."""

    def __init__(self, env: Any, game_action: Any, info: ArcToolkitInfo) -> None:
        self._env = env
        self._game_action = game_action
        self._info = info
        self._closed = False

    @property
    def info(self) -> ArcToolkitInfo:
        """What this connection runs on."""
        return self._info

    def reset(self) -> ToolkitFrame | None:
        """Call the toolkit's ``env.reset()``."""
        self._require_open()
        frame: ToolkitFrame | None = self._env.reset()
        return frame

    def step(
        self, action_id: int, data: Mapping[str, int] | None
    ) -> ToolkitFrame | None:
        """Call the toolkit's ``env.step`` with a ``GameAction``."""
        self._require_open()
        action = self._game_action.from_id(action_id)
        frame: ToolkitFrame | None = self._env.step(
            action, data=None if data is None else dict(data)
        )
        return frame

    def close(self) -> None:
        """Stop using the game. The scorecard is the evaluator's to close."""
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise ContractError("ARC game connection is closed")


def open_toolkit_game(  # pylint: disable=too-many-arguments
    game_id: str,
    *,
    seed: int = 0,
    operation_mode: str = "normal",
    base_url: str = ARC_API_BASE_URL,
    environments_dir: str = "environment_files",
    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
) -> ToolkitGameConnection:
    """Make one game with the official toolkit, within ``timeout_seconds``.

    ``Arcade`` resolves its final mode and base URL from the environment as
    well as these arguments; the connection records the values it settled on.
    """
    if not isinstance(game_id, str) or not game_id:
        raise ContractError("ARC game_id must be a non-empty string")
    arc_agi = require_arc_toolkit()
    from arcengine import GameAction  # pylint: disable=import-outside-toplevel

    def make() -> tuple[Any, Any]:
        arcade = arc_agi.Arcade(
            operation_mode=arc_agi.OperationMode(operation_mode),
            arc_base_url=base_url,
            environments_dir=environments_dir,
        )
        return arcade, arcade.make(game_id, seed=seed)

    arcade, env = TimedCaller(timeout_seconds).call(f"make {game_id}", make)
    if env is None:
        raise ArcCallError(f"the ARC toolkit could not make game {game_id!r}")
    info = ArcToolkitInfo(
        toolkit_version=importlib.metadata.version("arc-agi"),
        engine_version=importlib.metadata.version("arcengine"),
        game_id=game_id,
        seed=seed,
        operation_mode=str(arcade.operation_mode.value),
        base_url=str(arcade.arc_base_url),
    )
    return ToolkitGameConnection(env, GameAction, info)


def require_arc_toolkit() -> Any:
    """Return the ``arc_agi`` module or raise an installable-extra error.

    Typed as Any because the optional extra has no in-repo stubs.
    """
    try:
        import arc_agi  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ContractError(
            "ARC-AGI toolkit is missing; install vharness[arc] (Python 3.12+)"
        ) from exc
    return arc_agi
