"""The official ARC-AGI-3 scorecard, read strictly and never recomputed (COD-37).

The toolkit's scorecard is the only authority on completion and score. This
module parses one game's entry out of a scorecard dump into a typed record.
It copies the official numbers as they are: it never derives a score, a level
count or a completion flag of its own.

Anything missing, mistyped or ambiguous raises ``ContractError``; the
evaluator turns that into "not evaluated", never into a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..errors import ContractError
from ..models import JsonValue, frozen_mapping

#: Scorecard fields that are secrets and never stored, at any depth.
SCORECARD_SECRET_FIELDS = ("api_key",)


@dataclass(frozen=True, slots=True)
class ArcRunRecord:  # pylint: disable=too-many-instance-attributes
    """The scorecard's latest run of the game, as reported."""

    guid: str | None
    state: str | None
    score: float
    levels_completed: int
    actions: int
    resets: int
    level_scores: tuple[float, ...]
    level_actions: tuple[int, ...]
    level_baseline_actions: tuple[int, ...]

    def to_json(self) -> Mapping[str, JsonValue]:
        """JSON form for receipt objectives."""
        return frozen_mapping(
            {
                "guid": self.guid,
                "state": self.state,
                "score": self.score,
                "levels_completed": self.levels_completed,
                "actions": self.actions,
                "resets": self.resets,
                "level_scores": self.level_scores,
                "level_actions": self.level_actions,
                "level_baseline_actions": self.level_baseline_actions,
            }
        )


@dataclass(frozen=True, slots=True)
class ArcScorecard:  # pylint: disable=too-many-instance-attributes
    """One game's official entry on a scorecard."""

    card_id: str
    game_id: str
    completed: bool
    score: float
    levels_completed: int
    level_count: int
    actions: int
    resets: int
    latest_run: ArcRunRecord | None

    @classmethod
    def from_json(cls, raw: object, game_id: str) -> ArcScorecard:
        """Parse ``game_id``'s entry from a scorecard dump, strictly."""
        card = _mapping(raw, "scorecard")
        entries = [
            entry
            for entry in _sequence(card.get("environments"), "environments")
            if _mapping(entry, "environment").get("id") == game_id
        ]
        if not entries:
            raise ContractError(f"ARC scorecard has no entry for game {game_id!r}")
        if len(entries) > 1:
            raise ContractError(f"ARC scorecard lists game {game_id!r} twice")
        entry = _mapping(entries[0], "environment")
        runs = _sequence(entry.get("runs", []), "runs")
        level_count = _count(entry.get("level_count"), "level_count")
        levels_completed = _count(entry.get("levels_completed"), "levels_completed")
        if levels_completed > level_count:
            raise ContractError(
                f"ARC scorecard levels_completed {levels_completed} exceeds "
                f"level_count {level_count}"
            )
        return cls(
            card_id=_text(card.get("card_id"), "card_id"),
            game_id=game_id,
            completed=_flag(entry.get("completed"), "completed"),
            score=_score(entry.get("score"), "score"),
            levels_completed=levels_completed,
            level_count=level_count,
            actions=_count(entry.get("actions"), "actions"),
            resets=_count(entry.get("resets"), "resets"),
            latest_run=_run(runs[-1]) if runs else None,
        )

    def to_json(self) -> Mapping[str, JsonValue]:
        """The official measures, as reported, for receipt objectives."""
        return frozen_mapping(
            {
                "card_id": self.card_id,
                "game_id": self.game_id,
                "completed": self.completed,
                "score": self.score,
                "levels_completed": self.levels_completed,
                "level_count": self.level_count,
                "actions": self.actions,
                "resets": self.resets,
                "latest_run": (
                    None if self.latest_run is None else self.latest_run.to_json()
                ),
            }
        )


def redact_scorecard(raw: object) -> JsonValue:
    """A JSON-safe copy of a scorecard dump without secret fields, at any depth."""
    if isinstance(raw, Mapping):
        return frozen_mapping(
            {
                str(key): redact_scorecard(value)
                for key, value in raw.items()
                if key not in SCORECARD_SECRET_FIELDS
            }
        )
    if isinstance(raw, (list, tuple)):
        return tuple(redact_scorecard(item) for item in raw)
    if raw is None or isinstance(raw, (str, bool, int)):
        return raw
    if isinstance(raw, float) and math.isfinite(raw):
        return raw
    raise ContractError(f"ARC scorecard holds a non-JSON value {raw!r}")


def _run(raw: object) -> ArcRunRecord:
    run = _mapping(raw, "run")
    guid = run.get("guid")
    state = run.get("state")
    return ArcRunRecord(
        guid=None if guid is None else _text(guid, "run guid"),
        state=None if state is None else _text(state, "run state"),
        score=_score(run.get("score"), "run score"),
        levels_completed=_count(run.get("levels_completed"), "run levels_completed"),
        actions=_count(run.get("actions"), "run actions"),
        resets=_count(run.get("resets"), "run resets"),
        level_scores=tuple(
            _score(item, "run level score")
            for item in _sequence(run.get("level_scores", []), "run level_scores")
        ),
        level_actions=tuple(
            _count(item, "run level actions")
            for item in _sequence(run.get("level_actions", []), "run level_actions")
        ),
        level_baseline_actions=tuple(
            _count(item, "run baseline actions")
            for item in _sequence(
                run.get("level_baseline_actions", []), "run level_baseline_actions"
            )
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"ARC scorecard {name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"ARC scorecard {name} must be a list")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"ARC scorecard {name} must be a non-empty string")
    return value


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"ARC scorecard {name} must be a boolean, not {value!r}")
    return value


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(
            f"ARC scorecard {name} must be a non-negative integer, not {value!r}"
        )
    return value


def _score(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ContractError(
            f"ARC scorecard {name} must be a finite non-negative number, "
            f"not {value!r}"
        )
    return float(value)
