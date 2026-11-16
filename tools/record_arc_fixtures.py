"""Record ARC-AGI-3 conformance fixtures from one public game (COD-34).

Plays a public game in the toolkit's local mode with a seeded random policy
until it ends (or a step limit), and writes a JSON fixture holding:

* the toolkit versions, game id, seed and policy;
* the full action trace (every action and its x/y data);
* complete frames for the reset, the first few steps and the last step,
  each grid row stored as 64 hex digits (one per cell, colour 0-15);
* the scorecard, with its API key removed.

Adapter tests read the fixture with no network and no API key. Re-run this
when the pinned toolkit version changes. Needs the ``arc`` extra and network
access to the ARC API (``three.arcprize.org``).

Usage:
    python tools/record_arc_fixtures.py GAME_ID OUT_JSON [--seed N]
        [--max-steps N] [--keep-steps N] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DESCRIPTION = "Record ARC-AGI-3 conformance fixtures from one public game."
#: The toolkit's default base URL is arcprize.org; the ARC-AGI-3 API is here.
ARC_API_BASE_URL = "https://three.arcprize.org"
#: Scorecard fields that are secrets or session-specific and never recorded.
_SCORECARD_REDACT = ("api_key",)


@dataclass(frozen=True)
class RecordedFrame:
    """One toolkit frame as JSON-safe data."""

    state: str
    levels_completed: int
    win_levels: int
    available_actions: list[int]
    full_reset: bool
    action_input: dict[str, Any]
    #: Each grid is a list of rows; each row is one hex digit per cell.
    grids: list[list[str]]


@dataclass(frozen=True)
class RecordedStep:
    """One action and, for kept steps, the frame it produced."""

    index: int
    action: int
    data: dict[str, int] | None
    frame: RecordedFrame | None


@dataclass
class Recording:  # pylint: disable=too-many-instance-attributes
    """Everything one fixture file holds."""

    toolkit: dict[str, str]
    game_id: str
    seed: int
    policy: str
    reset: RecordedFrame
    steps: list[RecordedStep] = field(default_factory=list)
    final_state: str = ""
    scorecard: dict[str, Any] = field(default_factory=dict)


def frame_to_record(frame: Any) -> RecordedFrame:
    """Convert a toolkit ``FrameDataRaw`` into JSON-safe data."""
    return RecordedFrame(
        state=frame.state.value,
        levels_completed=int(frame.levels_completed),
        win_levels=int(frame.win_levels),
        available_actions=[int(a) for a in frame.available_actions],
        full_reset=bool(frame.full_reset),
        action_input=frame.action_input.model_dump(mode="json"),
        grids=[encode_grid(grid.tolist()) for grid in frame.frame],
    )


def encode_grid(rows: list[list[int]]) -> list[str]:
    """Encode a grid of colours 0-15 as one hex string per row."""
    encoded: list[str] = []
    for row in rows:
        if any(not 0 <= cell <= 15 for cell in row):
            raise ValueError(f"grid cell outside 0-15: {row}")
        encoded.append("".join(f"{cell:x}" for cell in row))
    return encoded


def decode_grid(rows: list[str]) -> list[list[int]]:
    """Inverse of ``encode_grid``."""
    return [[int(cell, 16) for cell in row] for row in rows]


def redact_scorecard(scorecard: dict[str, Any]) -> dict[str, Any]:
    """Drop secret fields from a scorecard dump, at any depth."""
    cleaned: dict[str, Any] = {}
    for key, value in scorecard.items():
        if key in _SCORECARD_REDACT:
            continue
        if isinstance(value, dict):
            value = redact_scorecard(value)
        elif isinstance(value, list):
            value = [redact_scorecard(v) if isinstance(v, dict) else v for v in value]
        cleaned[key] = value
    return cleaned


def record(  # pylint: disable=too-many-locals
    game_id: str, *, seed: int, max_steps: int, keep_steps: int
) -> Recording:
    """Play ``game_id`` with a seeded random policy and return the recording."""
    # Imported here so the module loads (and its helpers test) without the extra.
    from arc_agi import Arcade, OperationMode  # pylint: disable=import-outside-toplevel
    from arcengine import (  # pylint: disable=import-outside-toplevel
        GameAction,
        GameState,
    )

    arcade = Arcade(operation_mode=OperationMode.NORMAL, arc_base_url=ARC_API_BASE_URL)
    env = arcade.make(game_id, seed=seed)
    if env is None:
        raise RuntimeError(f"toolkit could not make game {game_id!r}")
    first = env.reset()
    if first is None:
        raise RuntimeError(f"reset of {game_id!r} returned no frame")
    recording = Recording(
        toolkit={
            name: importlib.metadata.version(name) for name in ("arc-agi", "arcengine")
        },
        game_id=game_id,
        seed=seed,
        policy=(
            f"random.Random({seed}); uniform over action_space; "
            "ACTION6 x,y uniform 0-63"
        ),
        reset=frame_to_record(first),
    )
    rng = random.Random(seed)
    frame = first
    for index in range(max_steps):
        action = rng.choice(env.action_space)
        data = (
            {"x": rng.randrange(64), "y": rng.randrange(64)}
            if action == GameAction.ACTION6
            else None
        )
        frame = env.step(action, data=data)
        if frame is None:
            raise RuntimeError(f"step {index} of {game_id!r} returned no frame")
        terminal = frame.state in (GameState.WIN, GameState.GAME_OVER)
        keep = index < keep_steps or terminal or index == max_steps - 1
        recording.steps.append(
            RecordedStep(
                index=index,
                action=int(
                    action.value[0] if isinstance(action.value, tuple) else action.value
                ),
                data=data,
                frame=frame_to_record(frame) if keep else None,
            )
        )
        if terminal:
            break
    recording.final_state = frame.state.value
    scorecard = arcade.get_scorecard()
    if scorecard is not None:
        recording.scorecard = redact_scorecard(scorecard.model_dump(mode="json"))
    return recording


def main(argv: list[str]) -> int:
    """Entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("game_id")
    parser.add_argument("out_json", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=400)
    parser.add_argument("--keep-steps", type=int, default=6)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path("arc-work"),
        help="where the toolkit downloads game files and writes recordings",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    out = args.out_json.resolve()
    args.workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(
        args.workdir
    )  # the toolkit uses relative environment_files/ and recordings/
    recording = record(
        args.game_id,
        seed=args.seed,
        max_steps=args.max_steps,
        keep_steps=args.keep_steps,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(recording), indent=1) + "\n", encoding="utf-8")
    print(
        f"recorded {args.game_id}: {len(recording.steps)} steps, final state "
        f"{recording.final_state}, written to {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
