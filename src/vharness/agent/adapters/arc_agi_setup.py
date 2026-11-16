"""Build the ARC-AGI-3 runtime and evaluator for one run (COD-38).

The composition root (``application``) calls this; the CLI only parses
arguments. One toolkit connection serves both the runtime (game actions) and
the evaluator (scorecard reads), so both see the same game and scorecard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..artifacts import ArtifactStore
from ..run_settings import ArcRunSettings
from .arc_agi_evaluator import ArcScorecardEvaluator
from .arc_agi_runtime import ArcRuntime
from .arc_agi_toolkit import ArcToolkitGame, open_toolkit_game

#: Evaluation contract recorded on ARC-AGI-3 tasks.
ARC_EVALUATION_CONTRACT = "arc-agi-3-scorecard-evaluator-v1"


def open_arc_game(settings: ArcRunSettings, games_dir: Path) -> ArcToolkitGame:
    """Open the toolkit game named by the settings."""
    return open_toolkit_game(
        settings.game_id,
        seed=settings.seed,
        operation_mode=settings.operation_mode,
        environments_dir=str(games_dir),
        timeout_seconds=settings.call_timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class ArcDependencies:
    """The ARC adapters for one run."""

    runtime: ArcRuntime
    evaluator: ArcScorecardEvaluator


# The composition step takes each explicit dependency by keyword.
def build_arc_dependencies(  # pylint: disable=too-many-arguments
    settings: ArcRunSettings,
    *,
    max_actions: int,
    games_dir: str | Path,
    artifacts: ArtifactStore,
    runtime_store_path: str | Path,
    opener: Callable[[ArcRunSettings, Path], ArcToolkitGame] = open_arc_game,
) -> ArcDependencies:
    """Open the game and build its runtime and scorecard evaluator."""
    directory = Path(games_dir)
    directory.mkdir(parents=True, exist_ok=True)
    connection = opener(settings, directory)
    try:
        runtime = ArcRuntime(
            connection,
            artifacts,
            runtime_store_path,
            max_actions=max_actions,
            call_timeout_seconds=settings.call_timeout_seconds,
        )
        evaluator = ArcScorecardEvaluator(
            connection,
            artifacts,
            call_timeout_seconds=settings.call_timeout_seconds,
        )
    except Exception:
        connection.close()
        raise
    return ArcDependencies(runtime, evaluator)
