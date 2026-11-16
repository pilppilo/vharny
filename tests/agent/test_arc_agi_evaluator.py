"""ARC-AGI-3 scorecard evaluator (COD-37), against the recorded scorecard."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from vharness.agent.adapters.arc_agi_evaluator import (
    COMPLETED,
    INCOMPLETE,
    NOT_EVALUATED,
    ArcScorecardEvaluator,
)
from vharness.agent.adapters.arc_agi_scorecard import ArcScorecard, redact_scorecard
from vharness.agent.adapters.arc_agi_toolkit import ArcToolkitInfo
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.errors import ContractError
from vharness.agent.models import EvaluationReceipt, EvaluationRequest, StateRef
from vharness.agent.ports import ReconciliationKind

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["scorecard"]
GAME_ID = "dc22-fdcac232"
INFO = ArcToolkitInfo(
    "0.9.10", "0.9.3", GAME_ID, 0, "normal", "https://three.arcprize.org"
)


class FakeSource:
    """Answers ``scorecard()`` with a scripted value, exception or block."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0
        self.release = threading.Event()

    @property
    def info(self) -> ArcToolkitInfo:
        return INFO

    def scorecard(self) -> Any:
        self.calls += 1
        if self.answer == "block":
            self.release.wait(5)
            return RECORDED
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _request(request_id: str = "eval-1", state: str = "cursor") -> EvaluationRequest:
    return EvaluationRequest(
        request_id=request_id,
        session_id="session-1",
        objective_version=1,
        attempt_id="attempt-1",
        evaluated_state=StateRef(f"arc-agi-3:{GAME_ID}", state, restorable=False),
        baseline_node_id="node-0",
        evaluator_version="1",
    )


def _card(**entry: Any) -> dict[str, Any]:
    """The recorded scorecard with the game's entry (and latest run) changed."""
    card = copy.deepcopy(RECORDED)
    game = card["environments"][0]
    run_changes = entry.pop("run", {})
    game.update(entry)
    game["runs"][-1].update(run_changes)
    return card


WIN = _card(
    completed=True,
    levels_completed=6,
    score=87.5,
    actions=640,
    run={"state": "WIN", "levels_completed": 6, "completed": True, "score": 87.5},
)
PARTIAL = _card(
    levels_completed=2,
    score=12.25,
    actions=301,
    run={"state": "GAME_OVER", "levels_completed": 2, "score": 12.25},
)


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _evaluate(artifacts: ArtifactStore, answer: Any, **options: Any):
    evaluator = ArcScorecardEvaluator(FakeSource(answer), artifacts, **options)
    return evaluator, evaluator.evaluate(_request())


def _objectives(receipt: EvaluationReceipt) -> dict[str, Any]:
    """The receipt's objectives as plain JSON data."""
    return json.loads(canonical_json(receipt.objectives))


def _stored(artifacts: ArtifactStore, receipt) -> dict[str, Any]:
    assert receipt.raw_receipt is not None
    return json.loads(artifacts.read(receipt.raw_receipt))


# ---- official verdicts --------------------------------------------------------


def test_win_is_accepted_with_the_scorecard_as_evidence(artifacts):
    _, receipt = _evaluate(artifacts, WIN)
    assert receipt.valid and receipt.accepted and receipt.completed
    assert receipt.comparison == COMPLETED
    assert receipt.evidence == (receipt.raw_receipt,)
    assert receipt.completion_evidence == receipt.evidence
    assert receipt.raw_receipt is not None
    assert receipt.required_evidence == (receipt.raw_receipt.digest,)
    assert _objectives(receipt)["score"] == 87.5
    assert _objectives(receipt)["levels_completed"] == 6
    assert _objectives(receipt)["level_count"] == 6
    assert _objectives(receipt)["actions"] == 640
    assert _objectives(receipt)["latest_run"]["state"] == "WIN"
    assert _stored(artifacts, receipt)["environments"][0]["completed"] is True
    assert receipt.usage is not None and receipt.usage.evaluations == 1


def test_partial_progress_is_valid_but_rejected(artifacts):
    _, receipt = _evaluate(artifacts, PARTIAL)
    assert receipt.valid and not receipt.accepted and not receipt.completed
    assert receipt.comparison == INCOMPLETE
    assert _objectives(receipt)["levels_completed"] == 2
    assert _objectives(receipt)["score"] == 12.25
    assert receipt.completion_evidence == () and receipt.required_evidence == ()
    assert receipt.evidence == (receipt.raw_receipt,)


def test_recorded_no_progress_reports_the_official_numbers(artifacts):
    _, receipt = _evaluate(artifacts, RECORDED)
    assert receipt.valid and not receipt.accepted
    objectives = _objectives(receipt)
    assert objectives["card_id"] == RECORDED["card_id"]
    assert objectives["score"] == 0.0 and objectives["levels_completed"] == 0
    assert objectives["actions"] == 127 and objectives["resets"] == 0
    run = objectives["latest_run"]
    assert run["state"] == "GAME_OVER"
    assert run["level_actions"] == [127, 0, 0, 0, 0, 0]
    assert run["level_baseline_actions"] == [59, 102, 67, 98, 324, 578]


def test_the_scorecard_not_the_state_decides(artifacts):
    """A state that claims a win cannot turn an incomplete scorecard into a pass."""
    evaluator = ArcScorecardEvaluator(FakeSource(RECORDED), artifacts)
    claim = json.dumps({"state": "WIN", "levels_completed": 6})
    receipt = evaluator.evaluate(_request(state=claim))
    assert not receipt.accepted and receipt.evaluated_state.value == claim


def test_numbers_are_copied_not_recomputed(artifacts):
    # Official numbers that do not add up are still reported as they are.
    odd = _card(completed=True, levels_completed=3, score=99.0, actions=5)
    _, receipt = _evaluate(artifacts, odd)
    assert receipt.accepted  # the scorecard says completed
    assert _objectives(receipt)["levels_completed"] == 3
    assert _objectives(receipt)["score"] == 99.0


# ---- not evaluated --------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, reason",
    [
        (None, "has no scorecard"),
        (ConnectionError("refused"), "get_scorecard raised ConnectionError: refused"),
        (_card(id="other-game"), "no entry for game 'dc22-fdcac232'"),
        ({"card_id": "c"}, "environments must be a list"),
        (_card(completed="yes"), "completed must be a boolean"),
        (_card(score=True), "score must be a finite non-negative number"),
        (_card(score=float("nan")), "non-JSON value"),
        (_card(actions=1.5), "actions must be a non-negative integer"),
        (_card(levels_completed=7), "exceeds level_count 6"),
        (_card(run={"level_actions": [1, -1]}), "run level actions"),
        ("not a scorecard", "scorecard must be an object"),
    ],
)
def test_missing_or_malformed_scorecards_are_never_a_pass(artifacts, answer, reason):
    _, receipt = _evaluate(artifacts, answer)
    assert not receipt.valid and not receipt.accepted and not receipt.completed
    assert receipt.comparison == NOT_EVALUATED
    assert reason in _objectives(receipt)["not_evaluated"]
    assert receipt.completion_evidence == () and receipt.required_evidence == ()


def test_a_malformed_scorecard_is_still_kept_as_evidence(artifacts):
    _, receipt = _evaluate(artifacts, _card(completed="yes"))
    assert receipt.evidence == (receipt.raw_receipt,)
    assert _stored(artifacts, receipt)["environments"][0]["completed"] == "yes"


def test_a_scorecard_that_times_out_is_not_evaluated(artifacts):
    source = FakeSource("block")
    evaluator = ArcScorecardEvaluator(source, artifacts, call_timeout_seconds=0.05)
    receipt = evaluator.evaluate(_request("eval-1"))
    assert not receipt.valid and "did not answer within 0.05s" in str(
        _objectives(receipt)["not_evaluated"]
    )
    busy = evaluator.evaluate(_request("eval-2"))
    assert not busy.valid and "still running" in str(_objectives(busy)["not_evaluated"])
    assert source.calls == 1
    source.release.set()


def test_two_entries_for_the_game_are_ambiguous():
    card = copy.deepcopy(RECORDED)
    card["environments"].append(copy.deepcopy(card["environments"][0]))
    with pytest.raises(ContractError, match="lists game 'dc22-fdcac232' twice"):
        ArcScorecard.from_json(card, GAME_ID)


# ---- secrets, identity, reconcile ---------------------------------------------


def test_secret_fields_are_never_stored(artifacts, tmp_path):
    secret = "scorecard-secret-api-key-42"
    card = _card()
    card["api_key"] = secret
    card["environments"][0]["runs"][0]["api_key"] = secret
    _, receipt = _evaluate(artifacts, card)
    assert receipt.valid
    for path in (tmp_path / "artifacts").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    assert "api_key" not in canonical_json(redact_scorecard(card))


def test_identity_pins_the_toolkit_setup(artifacts):
    first = ArcScorecardEvaluator(FakeSource(None), artifacts).identity()
    assert first.component_id == "arc-agi-3-scorecard-evaluator"
    assert first.version == "0.9.10"
    other = ArcScorecardEvaluator(
        FakeSource(None), artifacts, call_timeout_seconds=5
    ).identity()
    assert other.configuration_digest != first.configuration_digest


def test_reconcile_returns_receipts_it_produced(artifacts):
    evaluator, receipt = _evaluate(artifacts, PARTIAL)
    result = evaluator.reconcile("eval-1")
    assert result.kind is ReconciliationKind.RECEIPT and result.receipt == receipt
    assert evaluator.reconcile("never").kind is ReconciliationKind.UNKNOWN
