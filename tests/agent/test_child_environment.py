"""Commands run for the model must not see the harness's provider key."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent.adapters.child_environment import ChildEnvironment
from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.adapters.workspace_evaluator import (
    CheckCommandEvaluator,
    run_check_command,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError
from vharness.agent.models import (
    ActionProposal,
    EvaluationRequest,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)
from vharness.agent.run_settings import RunSettings

KEY_ENV = "SECRETKEY"
KEY_VALUE = "sk-must-not-leak"
PRINT_SECRETS = 'echo "key=${SECRETKEY:-absent} openai=${OPENAI_API_KEY:-absent} keep=${KEEP_ME:-absent}"'


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leak")
    monkeypatch.setenv("VHARNESS_PROVIDER_HEADERS", '{"x-session": "secret"}')
    monkeypatch.setenv("KEEP_ME", "visible")


def test_default_policy_withholds_key_and_provider_variables():
    parent = {
        KEY_ENV: KEY_VALUE,
        "OPENAI_API_KEY": "x",
        "VHARNESS_BASE_URL": "y",
        "PATH": "/usr/bin",
    }
    assert ChildEnvironment(KEY_ENV).build(parent) == {"PATH": "/usr/bin"}


def test_allow_list_passes_only_named_variables():
    parent = {KEY_ENV: KEY_VALUE, "PATH": "/usr/bin", "HOME": "/root"}
    assert ChildEnvironment(KEY_ENV, ("PATH", "MISSING")).build(parent) == {
        "PATH": "/usr/bin"
    }


def test_allow_list_cannot_include_the_key_variable():
    with pytest.raises(ContractError, match="cannot be passed"):
        ChildEnvironment(KEY_ENV, ("PATH", KEY_ENV))


def test_identity_does_not_depend_on_the_key_variable_name():
    assert (
        ChildEnvironment("ONE_KEY").describe() == ChildEnvironment("TWO_KEY").describe()
    )


def _proposal(proposal_id: str, action: str, arguments: dict[str, object]):
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-env",
        objective_version=1,
        attempt_id="attempt-env",
        action_name=action,
        arguments=arguments,
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="child environment test",
    )


def _payload(receipt, artifacts: ArtifactStore) -> dict[str, object]:
    assert receipt.raw_artifact is not None
    return json.loads(artifacts.read(receipt.raw_artifact))


def _exec_output(tmp_path: Path, environment: ChildEnvironment, *, pty: bool) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = ProcessRuntime(
        workspace,
        artifacts,
        tmp_path / "runtime.sqlite",
        timeout_seconds=5,
        excerpt_bytes=4096,
        child_environment=environment,
    )
    try:
        started = runtime.submit(
            _proposal("exec", "exec", {"command": PRINT_SECRETS, "pty": pty})
        )
        assert started.status is ExecutionStatus.SUCCEEDED
        job_id = _payload(started, artifacts)["job_id"]
        output = ""
        for index in range(50):
            read = _payload(
                runtime.submit(
                    _proposal(f"read-{index}", "job_read", {"job_id": job_id})
                ),
                artifacts,
            )
            output += str(read.get("stdout", ""))
            if "keep=" in output:
                return output
            time.sleep(0.05)
        raise AssertionError(f"command produced no output: {output!r}")
    finally:
        runtime.close(stop_jobs=True)


@pytest.mark.parametrize("pty", [False, True])
def test_exec_child_cannot_read_provider_key(tmp_path, secrets, pty):
    output = _exec_output(tmp_path, ChildEnvironment(KEY_ENV), pty=pty)
    assert KEY_VALUE not in output
    assert "key=absent openai=absent keep=visible" in output


def test_exec_child_gets_only_allow_listed_variables(tmp_path, secrets):
    output = _exec_output(tmp_path, ChildEnvironment(KEY_ENV, ("PATH",)), pty=False)
    assert "key=absent openai=absent keep=absent" in output


def test_check_command_cannot_read_provider_key(tmp_path, secrets):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    seen: dict[str, object] = {}

    def runner(command, cwd, timeout_seconds, env=None):
        seen["env"] = env
        return run_check_command(command, cwd, timeout_seconds, env=env)

    evaluator = CheckCommandEvaluator(
        command=("sh", "-c", PRINT_SECRETS),
        workspace=tmp_path,
        artifacts=artifacts,
        runner=runner,
        environment=ChildEnvironment(KEY_ENV),
    )
    request = EvaluationRequest(
        request_id="request-env",
        session_id="session-env",
        objective_version=1,
        attempt_id="attempt-env",
        evaluated_state=StateRef("workspace", "candidate", restorable=False),
        baseline_node_id="root-session-env",
        evaluator_version="check-v1",
    )
    receipt = evaluator.evaluate(request)
    stdout = artifacts.read(receipt.evidence[0]).decode()
    assert stdout.strip() == "key=absent openai=absent keep=visible"
    assert isinstance(seen["env"], dict) and KEY_ENV not in seen["env"]


def test_runtime_identity_records_the_policy(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")

    def digest(environment: ChildEnvironment) -> str:
        runtime = ProcessRuntime(
            workspace,
            artifacts,
            tmp_path / f"runtime-{id(environment)}.sqlite",
            child_environment=environment,
        )
        try:
            return runtime.identity().configuration_digest
        finally:
            runtime.close()

    default = digest(ChildEnvironment("ONE_KEY"))
    assert default == digest(ChildEnvironment("TWO_KEY"))
    assert default != digest(ChildEnvironment("ONE_KEY", ("PATH",)))


def _settings(**overrides) -> RunSettings:
    values: dict[str, object] = {
        "objective": "o",
        "workspace": "/tmp",
        "base_url": None,
        "model": None,
        "api_key_env": KEY_ENV,
        "max_actions": 1,
        "max_model_calls": 1,
        "max_evaluations": 1,
        "max_steps": 1,
        "token_budget": 1,
        "max_output_tokens": 1,
        "repair_budget": 0,
    }
    values.update(overrides)
    return RunSettings(**values)  # type: ignore[arg-type]


def test_run_settings_persist_the_allow_list():
    settings = _settings(child_env_allow=("PATH", "HOME"))
    restored = RunSettings.from_payload(settings.to_payload())
    assert restored.child_env_allow == ("PATH", "HOME")


def test_run_settings_from_older_manifest_use_default_policy():
    payload = _settings().to_payload()
    del payload["child_env_allow"]
    assert RunSettings.from_payload(payload).child_env_allow is None


def test_run_settings_reject_key_in_allow_list():
    with pytest.raises(ContractError, match="provider key"):
        _settings(child_env_allow=("PATH", KEY_ENV))
