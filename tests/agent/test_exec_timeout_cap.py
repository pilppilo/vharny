"""The model may lower, never raise, the operator's exec timeout (COD-31)."""

from __future__ import annotations

import json
import os

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent.adapters.process_jobs import DEFAULT_EXEC_TIMEOUT_SECONDS
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

CAP = 5


def _exec(proposal_id: str, **arguments: object) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-timeout",
        objective_version=1,
        attempt_id="attempt-timeout",
        action_name="exec",
        arguments={"command": "true", **arguments},
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="exec timeout cap test",
    )


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    rt = ProcessRuntime(
        workspace, artifacts, tmp_path / "runtime.sqlite", timeout_seconds=CAP
    )
    yield rt, artifacts
    rt.close(stop_jobs=True)


def _result(rt, artifacts, proposal: ActionProposal) -> dict[str, object]:
    receipt = rt.submit(proposal)
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert receipt.raw_artifact is not None
    return json.loads(artifacts.read(receipt.raw_artifact))


def test_request_above_the_cap_gets_the_cap(runtime):
    rt, artifacts = runtime
    result = _result(rt, artifacts, _exec("long", timeout_seconds=86_400))
    assert result["timeout_seconds"] == CAP
    assert result["timeout_requested"] == 86_400


def test_request_below_the_cap_is_kept(runtime):
    rt, artifacts = runtime
    result = _result(rt, artifacts, _exec("short", timeout_seconds=2))
    assert result["timeout_seconds"] == 2
    assert "timeout_requested" not in result


def test_no_request_uses_the_operator_timeout(runtime):
    rt, artifacts = runtime
    result = _result(rt, artifacts, _exec("default"))
    assert result["timeout_seconds"] == CAP


def test_exec_schema_declares_the_maximum(tmp_path):
    contract = WorkspaceEnvironment(tmp_path, max_exec_timeout_seconds=CAP).describe()
    [action] = [a for a in contract.actions if a.name == "exec"]
    schema = action.argument_schema
    assert schema["properties"]["timeout_seconds"]["maximum"] == CAP
    with pytest.raises(ContractError, match=f"must be <= {CAP}"):
        _validate_action_arguments({"command": "x", "timeout_seconds": CAP + 1}, schema)


def test_default_environment_and_runtime_agree(tmp_path):
    contract = WorkspaceEnvironment(tmp_path).describe()
    [action] = [a for a in contract.actions if a.name == "exec"]
    maximum = action.argument_schema["properties"]["timeout_seconds"]["maximum"]
    assert maximum == DEFAULT_EXEC_TIMEOUT_SECONDS
