"""ADR-0023: board-move labels for the supervisor (COD-93).

The replay test runs the saved ``ls20`` re-run through ``monitor.p7.v1``. The
other tests pin the labels on small boards, including the cases the trace does
not cover: a longer loop and a repeated move that does nothing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vharness.agent.application import build_supervisor
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.board_moves import BoardStep, MoveLabel, current_level
from vharness.agent.journal import SqliteJournal
from vharness.agent.run_settings import ArcRunSettings, RunSettings
from vharness.agent.supervision import (
    MONITOR_POLICY_P7_VERSION,
    MONITOR_POLICY_VERSION,
    action_fingerprint,
    board_fingerprints,
    board_level,
    decision_segments,
    evaluate_supervision,
    monitor_policy_p7,
)

RERUN = (
    Path(__file__).resolve().parents[2]
    / "evidence"
    / "phase-0004-longcat-trial"
    / "rerun-ls20"
)


def _rerun_events():
    journal = SqliteJournal(RERUN / "session.sqlite3", read_only=True)
    try:
        return journal.events("run-b2daf82b8b8e", limit=1000)
    finally:
        journal.close()


def test_ls20_detects_round_trip_without_flagging_forward_moves() -> None:
    events = _rerun_events()
    # Replay the decision inputs without carrying forward the old policy's cooldown.
    segments = decision_segments(
        tuple(event for event in events if event.kind != "supervision_triggered")
    )
    artifacts = ArtifactStore(RERUN / "artifacts")
    prefix = []
    actions = 0
    for call, segment in enumerate(segments, start=2):
        prefix.extend(segment)
        actions += sum(event.kind == "receipt_received" for event in segment)
        if actions not in (3, 6):
            continue
        trigger = evaluate_supervision(
            prefix,
            call,
            monitor_policy_p7(),
            artifacts=artifacts,
            model_call_budget=40,
        )
        if actions == 3:
            assert trigger is None or trigger.cause != "repetition"
        else:
            assert trigger is not None
            assert trigger.cause == "repetition"
            assert trigger.fingerprint is not None
            assert "Untried legal actions: [3, 4]" in trigger.fingerprint
            break
    assert actions == 6


def test_ls20_moves_are_labelled_forward_return_and_weak() -> None:
    level = board_level(_rerun_events(), ArtifactStore(RERUN / "artifacts"))
    assert level is not None
    labels = [move.label for move in level.moves]
    assert labels == [MoveLabel.FIRST, MoveLabel.FORWARD, MoveLabel.FORWARD] + [
        MoveLabel.RETURN
    ] * 6 + [MoveLabel.WEAK]
    assert level.untried_actions == (4,)  # action 3 was tried at move 10


def _board(row: int, column: int, tick: int) -> tuple[str, ...]:
    """An 8x8 board: a 2x2 piece at (row, column) and a moving counter cell."""
    cells = [["."] * 8 for _ in range(8)]
    for r in (row, row + 1):
        for c in (column, column + 1):
            cells[r][c] = "#"
    cells[7][tick % 8] = "*"
    return tuple("".join(line) for line in cells)


def _steps(*moves: tuple[int, int, int]) -> list[BoardStep]:
    """Steps from (action, row, column), with the counter advancing each move."""
    return [
        BoardStep(
            sequence=index,
            arguments={"action": action},
            board=_board(row, column, index),
            levels_completed=0,
            full_reset=False,
            legal_actions=(1, 2, 3, 4),
        )
        for index, (action, row, column) in enumerate(moves)
    ]


def test_a_four_move_loop_is_a_return() -> None:
    level = current_level(
        _steps((1, 0, 0), (2, 0, 3), (3, 3, 3), (4, 3, 0), (1, 0, 0), (2, 0, 3))
    )
    assert level is not None
    assert [move.label for move in level.moves] == [
        MoveLabel.FIRST,
        MoveLabel.FORWARD,
        MoveLabel.FORWARD,
        MoveLabel.FORWARD,
        MoveLabel.RETURN,
        MoveLabel.RETURN,
    ]
    assert level.untried_actions == ()


def test_repeating_a_move_that_does_nothing_still_counts_as_a_repeat() -> None:
    # Three real moves, then action 3 three times against a wall: only the
    # counter cell changes.
    level = current_level(
        _steps((1, 0, 0), (1, 2, 0), (1, 4, 0), (3, 4, 0), (3, 4, 0), (3, 4, 0))
    )
    assert level is not None
    labels = [move.label for move in level.moves]
    assert labels[1:3] == [MoveLabel.FORWARD, MoveLabel.FORWARD]
    assert labels[3:] == [MoveLabel.WEAK] * 3
    fingerprints = board_fingerprints(level, since_sequence=0)
    assert fingerprints.count(action_fingerprint("step", {"action": 3})) == 3
    assert action_fingerprint("step", {"action": 1}) not in fingerprints


def test_a_new_level_starts_a_fresh_history() -> None:
    steps = _steps((1, 0, 0), (2, 0, 3), (1, 0, 0))
    steps.append(replace(steps[-1], sequence=3, levels_completed=1))
    level = current_level(steps)
    assert level is not None
    assert [move.label for move in level.moves] == [MoveLabel.FIRST]
    assert level.untried_actions == (2, 3, 4)


@pytest.mark.parametrize("arc", [True, False])
def test_only_board_game_runs_use_the_board_policy(monkeypatch, tmp_path, arc):
    monkeypatch.setenv("TEST_KEY", "test-key")
    settings = RunSettings(
        objective="o",
        workspace=str(tmp_path),
        base_url="http://localhost:1/v1",
        model="m",
        api_key_env="TEST_KEY",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=1,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
        arc=ArcRunSettings("ls20-9607627b") if arc else None,
    )
    built = build_supervisor(settings)
    assert built is not None
    capability, close = built
    try:
        expected = MONITOR_POLICY_P7_VERSION if arc else MONITOR_POLICY_VERSION
        assert capability.policy.version == expected
    finally:
        close()


def test_the_default_policy_is_still_p6() -> None:
    assert MONITOR_POLICY_VERSION == "monitor.p6.v1"
