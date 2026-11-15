"""Check-command evaluator and operator decision construction contracts."""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

from vharness.agent.adapters.workspace_evaluator import (
    CHECK_EXIT_NONZERO,
    CHECK_TIMEOUT,
    CHECK_UNAVAILABLE,
    SURVIVORS_NONE,
    SURVIVORS_UNKNOWN,
    CheckCommandEvaluator,
    CheckOutcome,
    OperatorAnswer,
    operator_decision,
    run_check_command,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import parse_json_object
from vharness.agent.errors import ContractError
from vharness.agent.models import (
    EvaluationRequest,
    StateRef,
)
from vharness.agent.ports import ReconciliationKind


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        request_id="evaluation-check-1",
        session_id="session-check",
        objective_version=1,
        attempt_id="attempt-1",
        evaluated_state=StateRef("fake", "after", digest="1" * 64),
        baseline_node_id="root-session-check",
        evaluator_version="check-v1",
    )


def _evaluator(tmp_path: Path, outcome: CheckOutcome) -> CheckCommandEvaluator:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    return CheckCommandEvaluator(
        command=("true",),
        workspace=tmp_path,
        artifacts=artifacts,
        runner=lambda command, cwd, timeout: outcome,
    )


def test_exit_zero_is_accepted_with_native_evidence(tmp_path):
    evaluator = _evaluator(
        tmp_path, CheckOutcome("exit-zero", 0, b"all tests passed", b"")
    )
    receipt = evaluator.evaluate(_request())
    assert receipt.accepted is True
    assert receipt.comparison == "check exit-zero"
    assert receipt.evaluator_version == "check-v1"
    assert receipt.request_id == "evaluation-check-1"
    assert receipt.baseline_node_id == "root-session-check"
    assert receipt.evaluated_state.digest == "1" * 64
    assert len(receipt.evidence) == 2
    stdout = evaluator.artifacts.read(receipt.evidence[0])
    assert stdout == b"all tests passed"


def test_identical_reruns_share_receipt_identity(tmp_path):
    first = _evaluator(tmp_path, CheckOutcome("exit-zero", 0, b"same", b""))
    second = _evaluator(tmp_path, CheckOutcome("exit-zero", 0, b"same", b""))
    assert (
        first.evaluate(_request()).receipt_id == second.evaluate(_request()).receipt_id
    )
    changed = _evaluator(tmp_path, CheckOutcome("exit-zero", 0, b"different", b""))
    assert (
        changed.evaluate(_request()).receipt_id != first.evaluate(_request()).receipt_id
    )


@pytest.mark.parametrize(
    "outcome",
    [
        CheckOutcome(CHECK_EXIT_NONZERO, 1, b"", b"failure"),
        CheckOutcome(CHECK_TIMEOUT, None, b"partial", b""),
        CheckOutcome(CHECK_UNAVAILABLE, None, b"", b"no such tool"),
    ],
)
def test_failures_timeouts_and_missing_checkers_never_pass(tmp_path, outcome):
    evaluator = _evaluator(tmp_path, outcome)
    receipt = evaluator.evaluate(_request())
    assert receipt.accepted is False
    assert receipt.completed is False
    assert outcome.kind in receipt.comparison


def test_reconcile_reports_unknown(tmp_path):
    evaluator = _evaluator(tmp_path, CheckOutcome("exit-zero", 0, b"", b""))
    result = evaluator.reconcile("evaluation-check-1")
    assert result.kind is ReconciliationKind.UNKNOWN


def test_native_result_artifact_retains_command_and_exit_code(tmp_path):
    evaluator = _evaluator(tmp_path, CheckOutcome(CHECK_EXIT_NONZERO, 7, b"", b"boom"))
    receipt = evaluator.evaluate(_request())
    assert receipt.accepted is False
    assert receipt.raw_receipt is not None
    native = parse_json_object(evaluator.artifacts.read(receipt.raw_receipt))
    assert native["exit_code"] == 7
    assert native["kind"] == CHECK_EXIT_NONZERO
    assert native["command"] == ["true"]
    assert native["survivors"] == SURVIVORS_NONE
    assert native["stderr_digest"] == receipt.evidence[1].digest


def test_timeout_escalates_when_a_child_ignores_sigterm(tmp_path):
    marker = tmp_path / "ignoring-child-marker"
    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(6.0)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write('survived')\n"
    )
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    outcome = run_check_command(
        (sys.executable, "-c", parent, child, str(marker)), tmp_path, 0.5
    )
    elapsed = time.monotonic() - started
    assert outcome.kind == CHECK_TIMEOUT
    assert elapsed < 3.5
    assert outcome.survivors == SURVIVORS_NONE
    assert not marker.exists()


def test_escaped_descendant_bounds_the_drain_and_reports_unknown(tmp_path):
    child = "import os, time\n" "os.setsid()\n" "time.sleep(6.0)\n"
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1]])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    outcome = run_check_command((sys.executable, "-c", parent, child), tmp_path, 0.5)
    elapsed = time.monotonic() - started
    assert outcome.kind == CHECK_TIMEOUT
    assert outcome.survivors == SURVIVORS_UNKNOWN
    assert elapsed < 3.5


def test_timeout_terminates_the_whole_process_group(tmp_path):
    marker = tmp_path / "child-marker"
    child = (
        "import sys, time\n"
        "time.sleep(1.0)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write('survived')\n"
    )
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n"
    )
    outcome = run_check_command(
        (sys.executable, "-c", parent, child, str(marker)), tmp_path, 0.5
    )
    assert outcome.kind == CHECK_TIMEOUT
    time.sleep(1.5)
    assert not marker.exists()
    assert outcome.survivors == SURVIVORS_NONE


def test_operator_decision_publishes_evidence(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    evidence = tmp_path / "note.md"
    evidence.write_text("manual verification notes", encoding="utf-8")
    decision = operator_decision(
        command_id="decision-1",
        session_id="session-check",
        request_id="evaluation-check-1",
        answer=OperatorAnswer(
            accepted=True,
            reason="milestone verified by hand",
            evidence_path=evidence,
        ),
        artifacts=artifacts,
    )
    assert decision.evidence_ref is not None
    assert artifacts.read(decision.evidence_ref) == b"manual verification notes"


def test_operator_decision_validation(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ContractError):
        operator_decision(
            command_id="decision-2",
            session_id="session-check",
            request_id="evaluation-check-1",
            answer=OperatorAnswer(accepted=True, reason="   "),
            artifacts=artifacts,
        )
    with pytest.raises(ContractError):
        operator_decision(
            command_id="decision-3",
            session_id="session-check",
            request_id="evaluation-check-1",
            answer=OperatorAnswer(accepted=True, reason="accepts without evidence"),
            artifacts=artifacts,
        )
