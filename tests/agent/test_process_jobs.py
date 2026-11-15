"""Real process-group, output, stdin, PTY, and timeout contract coverage."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.models import (
    ActionProposal,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)


def _proposal(
    proposal_id: str, action: str, arguments: dict[str, object]
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-jobs",
        objective_version=1,
        attempt_id="attempt-jobs",
        action_name=action,
        arguments=arguments,
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="process job test",
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
        yield instance, artifacts
    finally:
        instance.close(stop_jobs=True)


def _payload(receipt, artifacts: ArtifactStore) -> dict[str, object]:
    assert receipt.raw_artifact is not None
    return json.loads(artifacts.read(receipt.raw_artifact))


def test_pipe_job_streams_output_accepts_input_and_eof(runtime) -> None:
    worker, artifacts = runtime
    start = worker.submit(
        _proposal(
            "pipe",
            "exec",
            {
                "command": f"{sys.executable} -c 'import sys; print(\"ready\", flush=True); print(sys.stdin.readline().strip(), file=sys.stderr)'"
            },
        )
    )
    assert start.status is ExecutionStatus.SUCCEEDED
    job_id = _payload(start, artifacts)["job_id"]
    first = {"stdout": "", "stdout_next_offset": 0, "stderr_next_offset": 0}
    for index in range(20):
        first = _payload(
            worker.submit(
                _proposal(f"pipe-read-{index}", "job_read", {"job_id": job_id})
            ),
            artifacts,
        )
        if first["stdout"] or first["status"] == "exited":
            break
        time.sleep(0.05)
    assert "ready" in first["stdout"]
    worker.submit(
        _proposal("pipe-input", "job_write", {"job_id": job_id, "input": "hello\n"})
    )
    worker.submit(
        _proposal("pipe-eof", "job_write", {"job_id": job_id, "input": "", "eof": True})
    )
    time.sleep(0.05)
    final = _payload(
        worker.submit(
            _proposal(
                "pipe-final",
                "job_read",
                {
                    "job_id": job_id,
                    "stdout_offset": first["stdout_next_offset"],
                    "stderr_offset": first["stderr_next_offset"],
                },
            )
        ),
        artifacts,
    )
    assert "hello" in final["stderr"]
    assert final["status"] == "exited"
    assert final["exit_code"] == 0
    assert final["output_artifacts"]
    first_reference = final["output_artifacts"][0]
    assert first_reference["stream"] in {"stdout", "stderr"}
    assert first_reference["offset"] >= 0
    assert first_reference["artifact"]["digest"]
    page = _payload(
        worker.submit(
            _proposal(
                "pipe-artifact-page",
                "job_read",
                {"job_id": job_id, "artifact_offset": 0, "artifact_limit": 1},
            )
        ),
        artifacts,
    )
    assert len(page["output_artifacts"]) == 1
    assert page["artifact_next_offset"] == 1
    assert page["artifact_total"] >= 1


def test_linux_pty_supports_controlling_terminal_and_interactive_input(runtime) -> None:
    worker, artifacts = runtime
    start = worker.submit(
        _proposal(
            "pty",
            "exec",
            {
                "command": f'{sys.executable} -c \'open("/dev/tty", "w").write("tty-ok\\n"); input("prompt> " ); print("received")\'',
                "pty": True,
            },
        )
    )
    job_id = _payload(start, artifacts)["job_id"]
    initial = {"stdout": ""}
    for index in range(20):
        initial = _payload(
            worker.submit(
                _proposal(f"pty-initial-{index}", "job_read", {"job_id": job_id})
            ),
            artifacts,
        )
        if "prompt>" in initial["stdout"]:
            break
        time.sleep(0.05)
    assert "tty-ok" in initial["stdout"]
    worker.submit(
        _proposal("pty-input", "job_write", {"job_id": job_id, "input": "answer\n"})
    )
    result = {"stdout": "", "status": "running"}
    for index in range(20):
        result = _payload(
            worker.submit(
                _proposal(f"pty-after-input-{index}", "job_read", {"job_id": job_id})
            ),
            artifacts,
        )
        if result["status"] == "exited":
            break
        time.sleep(0.05)
    assert "answer" in result["stdout"]
    assert "received" in result["stdout"]


def test_owned_group_stop_and_configured_timeout(runtime, tmp_path: Path) -> None:
    worker, artifacts = runtime
    started = worker.submit(_proposal("stop-start", "exec", {"command": "sleep 20"}))
    job_id = _payload(started, artifacts)["job_id"]
    stopped = worker.submit(_proposal("stop-action", "job_stop", {"job_id": job_id}))
    assert stopped.status is ExecutionStatus.SUCCEEDED
    assert _payload(stopped, artifacts)["status"] == "exited"

    timeout_worker = ProcessRuntime(
        worker.workspace,
        worker.artifacts,
        tmp_path / "timeout.sqlite",
        timeout_seconds=0.1,
    )
    try:
        timed = timeout_worker.submit(
            _proposal(
                "timeout-start",
                "exec",
                {"command": "sleep 5"},
            )
        )
        timed_job_id = _payload(timed, artifacts)["job_id"]
        time.sleep(0.15)
        result = timeout_worker.submit(
            _proposal("timeout-read", "job_read", {"job_id": timed_job_id})
        )
        timeout_result = _payload(result, artifacts)
        assert timeout_result["timed_out"] is True
        assert timeout_result["status"] == "exited"
    finally:
        timeout_worker.close(stop_jobs=True)
