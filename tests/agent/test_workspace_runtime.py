"""Workspace file, durable dispatch, and existing Session port integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import subprocess
import time

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent import BudgetLimits, CompletionMode, Session, TaskSpec
from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.adapters.runtime_store import proposal_fingerprint
from vharness.agent.adapters.workspace import WorkspaceEnvironment
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ActionProposal,
    ComponentIdentity,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)
from vharness.agent.ports import ReconciliationKind, EvaluationReconciliationResult
from vharness.agent.models import EvaluationReceipt, EvaluationRequest


class _UnusedEvaluator:
    """Evaluator port for an integration test that does not request grading."""

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("test-evaluator", "1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        raise AssertionError(f"unexpected evaluation request: {request.request_id}")

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        del request_id
        raise AssertionError("unexpected evaluation reconciliation")


def _proposal(
    proposal_id: str, action: str, arguments: dict[str, object]
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-1",
        objective_version=1,
        attempt_id="attempt-1",
        action_name=action,
        arguments=arguments,
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="runtime contract test",
    )


@pytest.fixture
def runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    instance = ProcessRuntime(
        workspace,
        artifacts,
        tmp_path / "runtime.sqlite",
        timeout_seconds=3,
        excerpt_bytes=4096,
    )
    try:
        yield instance, workspace, artifacts
    finally:
        instance.close(stop_jobs=True)


def _payload(receipt, artifacts: ArtifactStore) -> dict[str, object]:
    assert receipt.raw_artifact is not None
    return json.loads(artifacts.read(receipt.raw_artifact))


def test_workspace_read_write_preserves_prior_and_new_artifacts(runtime) -> None:
    worker, workspace, artifacts = runtime
    (workspace / "keep.txt").write_text("preserve", encoding="utf-8")
    (workspace / "large.txt").write_text("x" * 8192, encoding="utf-8")

    write = worker.submit(
        _proposal("write-1", "write", {"path": "new.txt", "content": "hello"})
    )
    result = _payload(write, artifacts)
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "preserve"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hello"
    assert result["written"]["size"] == 5

    read = worker.submit(
        _proposal("read-1", "read", {"path": "new.txt", "start": 1, "length": 3})
    )
    assert _payload(read, artifacts)["excerpt"] == "ell"
    assert read.usage is not None and read.usage.actions == 1
    assert read.state is not None and not read.state.restorable
    assert read.state.value != write.state.value

    bounded = worker.submit(
        _proposal("read-bounded", "read", {"path": "large.txt", "length": 8192})
    )
    bounded_payload = _payload(bounded, artifacts)
    assert len(bounded_payload["excerpt"]) == 4096
    assert bounded_payload["truncated"] is True


def test_dispatch_identity_replay_conflict_and_unknown_gap(
    runtime, tmp_path: Path
) -> None:
    worker, workspace, artifacts = runtime
    proposal = _proposal("once", "write", {"path": "one.txt", "content": "1"})
    first = worker.submit(proposal)
    assert worker.submit(proposal) == first
    with pytest.raises(ContractError):
        worker.submit(_proposal("once", "write", {"path": "one.txt", "content": "2"}))

    reopened = ProcessRuntime(workspace, artifacts, tmp_path / "dispatch-gap.sqlite")
    try:
        gap = _proposal("gap", "exec", {"command": "sleep 2"})
        reopened.store.prepare(gap.proposal_id, proposal_fingerprint(gap))
        assert reopened.reconcile(gap.proposal_id).kind is ReconciliationKind.UNKNOWN
        uncertain = reopened.submit(gap)
        assert uncertain.status is ExecutionStatus.INDETERMINATE
        assert uncertain.usage is None
    finally:
        reopened.close()


def test_invalid_action_argument_is_a_durable_rejection(
    runtime, tmp_path: Path
) -> None:
    worker, _workspace_dir, artifacts = runtime
    rejected = worker.submit(_proposal("read-missing", "read", {"path": "absent.txt"}))

    assert rejected.status is ExecutionStatus.REJECTED
    assert (
        "read path must name an existing file" in _payload(rejected, artifacts)["error"]
    )
    assert rejected.usage is not None and rejected.usage.actions == 1

    reconciled = worker.reconcile("read-missing")
    assert reconciled.kind is ReconciliationKind.RECEIPT
    assert reconciled.receipt == rejected

    reopened = ProcessRuntime(_workspace_dir, artifacts, tmp_path / "runtime.sqlite")
    try:
        durable = reopened.reconcile("read-missing")
        assert durable.kind is ReconciliationKind.RECEIPT
        assert durable.receipt is not None
        assert durable.receipt.status is ExecutionStatus.REJECTED
    finally:
        reopened.close()


def test_existing_session_accepts_runtime_receipts_and_trajectory_cursor(
    runtime, tmp_path: Path
) -> None:
    worker, workspace, artifacts = runtime
    environment = WorkspaceEnvironment(workspace, artifacts)
    journal = SqliteJournal(tmp_path / "session.sqlite")
    evaluator = _UnusedEvaluator()
    session = Session.create(
        "workspace-session",
        TaskSpec(
            task_id="workspace-task",
            objective="write and read a small file",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluation",
            budgets=BudgetLimits(actions=50),
            environment_id="local-workspace",
            evaluation_contract_id="test-evaluation-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=worker,
        evaluator=evaluator,
        initial_observation=environment.initial_observation("workspace-session"),
    )
    session.begin_attempt("workspace-attempt")
    receipt = session.submit_action(
        "write",
        {"path": "from-session.txt", "content": "written"},
        rationale="write file",
    )
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert receipt.operation_id == receipt.proposal_id
    assert receipt.state is not None and not receipt.state.restorable
    assert receipt.usage is not None and receipt.usage.actions == 1
    assert (workspace / "from-session.txt").read_text(encoding="utf-8") == "written"

    started = session.submit_action(
        "exec",
        {
            "command": (
                f'{sys.executable} -c \'import sys; print("session-ready", '
                "flush=True); print(sys.stdin.readline().strip(), flush=True)'"
            )
        },
        rationale="start an interactive workspace command",
        proposal_id="workspace-exec",
    )
    job_id = _payload(started, artifacts)["job_id"]
    assert started.status is ExecutionStatus.SUCCEEDED
    assert started.operation_id == started.proposal_id
    output = {"stdout": ""}
    for index in range(20):
        output_receipt = session.submit_action(
            "job_read",
            {"job_id": job_id},
            rationale="read command output",
            proposal_id=f"workspace-read-{index}",
        )
        output = _payload(output_receipt, artifacts)
        if "session-ready" in output["stdout"]:
            break
        time.sleep(0.05)
    assert "session-ready" in output["stdout"]
    written_input = session.submit_action(
        "job_write",
        {"job_id": job_id, "input": "session-input\n"},
        rationale="send command input",
        proposal_id="workspace-input",
    )
    assert written_input.status is ExecutionStatus.SUCCEEDED
    session.submit_action(
        "job_write",
        {"job_id": job_id, "input": "", "eof": True},
        rationale="close command input",
        proposal_id="workspace-eof",
    )
    result_payload = {"status": "running", "stdout": ""}
    result = None
    for index in range(20):
        result = session.submit_action(
            "job_read",
            {
                "job_id": job_id,
                "stdout_offset": output["stdout_next_offset"],
                "stderr_offset": output["stderr_next_offset"],
            },
            rationale="read the completed command output",
            proposal_id=f"workspace-result-{index}",
        )
        result_payload = _payload(result, artifacts)
        if result_payload["status"] == "exited":
            break
        time.sleep(0.05)
    assert result is not None
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result_payload["status"] == "exited"
    assert "session-input" in result_payload["stdout"]
    stopped = session.submit_action(
        "job_stop",
        {"job_id": job_id},
        rationale="confirm job stop outcome",
        proposal_id="workspace-stop",
    )
    assert stopped.status is ExecutionStatus.SUCCEEDED
    assert stopped.operation_id == stopped.proposal_id
    assert "receipt_received" in [event.kind for event in session.events()]
    journal.close()


def test_owner_process_death_leaves_effect_unknown_without_retry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "owner-workspace"
    workspace.mkdir()
    artifacts = tmp_path / "owner-artifacts"
    database = tmp_path / "owner-runtime.sqlite"
    marker = workspace / "ran.txt"
    command = f"printf 'ran\\n' >> {marker}"
    child_source = f"""
import os
from pathlib import Path
from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.models import ActionProposal, ResourceReservation, StateRef

workspace = Path({str(workspace)!r})
runtime = ProcessRuntime(workspace, ArtifactStore({str(artifacts)!r}), {str(database)!r})
proposal = ActionProposal(
    'owner-death', 'session-1', 1, 'attempt-1', 'exec',
    {{'command': {command!r}}}, StateRef('workspace', 'initial', restorable=False),
    ResourceReservation('reservation-owner-death', 'owner-death', 1), 'runtime contract test'
)
runtime.store.finish = lambda receipt, fingerprint: os._exit(23)
runtime.submit(proposal)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_source],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert child.returncode == 23, child.stderr
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.read_text(encoding="utf-8") == "ran\n"

    recovered = ProcessRuntime(workspace, ArtifactStore(artifacts), database)
    try:
        retry = recovered.submit(_proposal("owner-death", "exec", {"command": command}))
        assert retry.status is ExecutionStatus.INDETERMINATE
        assert retry.usage is None
        time.sleep(0.2)
        assert marker.read_text(encoding="utf-8") == "ran\n"
    finally:
        recovered.close(stop_jobs=True)


def test_workspace_contract_advertises_irreversible_cursor(runtime) -> None:
    _, workspace, _ = runtime
    contract = WorkspaceEnvironment(workspace).describe()
    assert {action.name for action in contract.actions} == {
        "read",
        "write",
        "exec",
        "job_read",
        "job_write",
        "job_stop",
    }
    assert contract.capabilities.idempotency is True
    assert contract.capabilities.reconciliation is True
    assert contract.capabilities.cancellation is False
    assert contract.capabilities.revisions is False
