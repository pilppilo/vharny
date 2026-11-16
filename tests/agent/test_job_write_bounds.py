"""job_write must not hang the session when a process stops reading (COD-29)."""

from __future__ import annotations

import json
import os
import time

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent.adapters import process_jobs
from vharness.agent.adapters.process_jobs import MAX_WRITE_BYTES
from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.adapters.workspace import WorkspaceEnvironment
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError
from vharness.agent.models import (
    ActionProposal,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)
from vharness.agent.session import _validate_action_arguments

BOUND = 0.5  # shortened write timeout for the tests


def _proposal(proposal_id: str, action: str, arguments: dict[str, object]):
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-write",
        objective_version=1,
        attempt_id="attempt-write",
        action_name=action,
        arguments=arguments,
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="job_write bound test",
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(process_jobs, "WRITE_TIMEOUT_SECONDS", BOUND)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    rt = ProcessRuntime(
        workspace,
        artifacts,
        tmp_path / "runtime.sqlite",
        timeout_seconds=30,
        excerpt_bytes=4096,
    )
    yield rt, artifacts
    rt.close(stop_jobs=True)


def _start_non_reader(rt, artifacts, *, pty: bool) -> str:
    receipt = rt.submit(_proposal("exec", "exec", {"command": "sleep 30", "pty": pty}))
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert receipt.raw_artifact is not None
    return str(json.loads(artifacts.read(receipt.raw_artifact))["job_id"])


def _write(rt, proposal_id: str, job_id: str, text: str, *, eof: bool = False):
    started = time.monotonic()
    receipt = rt.submit(
        _proposal(
            proposal_id, "job_write", {"job_id": job_id, "input": text, "eof": eof}
        )
    )
    return receipt, time.monotonic() - started


def test_pipe_write_to_a_process_that_never_reads_returns_within_the_bound(runtime):
    rt, artifacts = runtime
    job_id = _start_non_reader(rt, artifacts, pty=False)
    first, _ = _write(rt, "w1", job_id, "x" * MAX_WRITE_BYTES)  # fills the pipe buffer
    assert first.status is ExecutionStatus.SUCCEEDED
    second, elapsed = _write(rt, "w2", job_id, "y" * 1000, eof=True)
    assert elapsed < BOUND + 2
    assert second.status is ExecutionStatus.FAILED
    assert "accepted 0 of 1000 bytes" in (second.error or "")
    assert "EOF was not sent" in (second.error or "")


def test_pty_write_to_a_process_that_never_reads_returns_within_the_bound(runtime):
    rt, artifacts = runtime
    job_id = _start_non_reader(rt, artifacts, pty=True)
    lines = ("z" * 99 + "\n") * 600  # line input fills the terminal's buffer
    receipt, elapsed = _write(rt, "w1", job_id, lines)
    assert elapsed < BOUND + 2
    assert receipt.status is ExecutionStatus.FAILED
    assert "job_write timed out" in (receipt.error or "")
    assert f"of {len(lines)} bytes" in (receipt.error or "")


def test_write_over_the_limit_is_rejected(runtime):
    rt, artifacts = runtime
    job_id = _start_non_reader(rt, artifacts, pty=False)
    receipt, _ = _write(rt, "w1", job_id, "x" * (MAX_WRITE_BYTES + 1))
    assert receipt.status is ExecutionStatus.REJECTED
    assert f"the limit is {MAX_WRITE_BYTES}" in (receipt.error or "")


def test_write_to_a_reading_process_still_succeeds(runtime):
    rt, artifacts = runtime
    receipt = rt.submit(_proposal("exec", "exec", {"command": "cat > /dev/null"}))
    job_id = str(json.loads(artifacts.read(receipt.raw_artifact))["job_id"])
    written, _ = _write(rt, "w1", job_id, "hello\n" * 10_000, eof=True)
    assert written.status is ExecutionStatus.SUCCEEDED


def test_action_schema_declares_the_input_limit(tmp_path):
    contract = WorkspaceEnvironment(tmp_path).describe()
    [action] = [a for a in contract.actions if a.name == "job_write"]
    assert action.argument_schema["properties"]["input"]["maxLength"] == MAX_WRITE_BYTES


def test_schema_validator_enforces_max_length():
    schema = {
        "type": "object",
        "properties": {"input": {"type": "string", "maxLength": 3}},
        "required": ["input"],
    }
    _validate_action_arguments({"input": "abc"}, schema)
    with pytest.raises(ContractError, match="at most 3 characters"):
        _validate_action_arguments({"input": "abcd"}, schema)
