"""Noisy jobs finish without the model reading every byte (COD-30)."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

if os.name != "posix":
    pytest.skip("local process runtime requires POSIX", allow_module_level=True)

from vharness.agent.adapters.process_jobs import KEEP_HEAD_BYTES, KEEP_TAIL_BYTES
from vharness.agent.adapters.process_runtime import ProcessRuntime
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.models import (
    ActionProposal,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)

PY = sys.executable


def _proposal(proposal_id: str, action: str, arguments: dict[str, object]):
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-drain",
        objective_version=1,
        attempt_id="attempt-drain",
        action_name=action,
        arguments=arguments,
        expected_state=StateRef("workspace", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="job output drain test",
    )


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    rt = ProcessRuntime(
        workspace,
        artifacts,
        tmp_path / "runtime.sqlite",
        timeout_seconds=60,
    )
    yield rt, artifacts
    rt.close(stop_jobs=True)


def _submit(rt, artifacts, proposal_id, action, arguments) -> dict[str, object]:
    receipt = rt.submit(_proposal(proposal_id, action, arguments))
    assert receipt.status is ExecutionStatus.SUCCEEDED, receipt.error
    assert receipt.raw_artifact is not None
    return json.loads(artifacts.read(receipt.raw_artifact))


def _start(rt, artifacts, script: str) -> str:
    command = f"{PY} -c '{script}'"
    return str(_submit(rt, artifacts, "exec", "exec", {"command": command})["job_id"])


def _status_after_other_actions(rt, artifacts, job_id: str, actions: int) -> str:
    """Run unrelated actions, then report the job's status without reading it."""
    for index in range(actions):
        _submit(rt, artifacts, f"other-{index}", "read", {"path": "note.txt"})
    return str(
        _submit(rt, artifacts, "status", "job_read", {"job_id": job_id})["status"]
    )


def test_3mb_producer_finishes_within_two_actions(runtime):
    rt, artifacts = runtime
    job_id = _start(
        rt,
        artifacts,
        'import sys; sys.stdout.write("x" * 3_000_000); sys.stdout.flush()',
    )
    time.sleep(0.2)
    assert _status_after_other_actions(rt, artifacts, job_id, actions=1) == "exited"


def test_endless_producer_cannot_hang_an_action(runtime):
    rt, artifacts = runtime
    job_id = _start(
        rt, artifacts, 'import sys\nwhile True: sys.stdout.write("y" * 4096)'
    )
    started = time.monotonic()
    result = _submit(rt, artifacts, "r1", "job_read", {"job_id": job_id})
    assert time.monotonic() - started < 5
    assert result["status"] == "running"
    _submit(rt, artifacts, "stop", "job_stop", {"job_id": job_id})


def test_truncation_keeps_head_and_tail_and_reports_dropped_bytes(runtime):
    rt, artifacts = runtime
    filler = KEEP_HEAD_BYTES + 2 * KEEP_TAIL_BYTES
    job_id = _start(
        rt,
        artifacts,
        "import sys; w = sys.stdout.write; "
        f'w("START\\n"); w("z" * {filler}); w("\\nEND-MARKER\\n"); sys.stdout.flush()',
    )
    for index in range(10):
        result = _submit(rt, artifacts, f"r{index}", "job_read", {"job_id": job_id})
        if result["status"] == "exited":
            break
        time.sleep(0.1)
    assert result["status"] == "exited"
    assert result["stdout_dropped_bytes"] > 0
    head = _submit(
        rt, artifacts, "head", "job_read", {"job_id": job_id, "stdout_offset": 0}
    )
    assert str(head["stdout"]).startswith("START")
    offset, pages = KEEP_HEAD_BYTES, []
    while True:
        page = _submit(
            rt,
            artifacts,
            f"tail-{offset}",
            "job_read",
            {"job_id": job_id, "stdout_offset": offset},
        )
        if not page["stdout"]:
            break
        pages.append(str(page["stdout"]))
        offset = int(page["stdout_next_offset"])  # type: ignore[arg-type]
    assert f"{result['stdout_dropped_bytes']} bytes of stdout were dropped" in pages[0]
    assert pages[-1].rstrip().endswith("END-MARKER")
    kept_tail = "".join(pages).split("follow]\n", 1)[1]
    assert len(kept_tail.encode()) == KEEP_TAIL_BYTES


def test_small_output_is_not_truncated(runtime):
    rt, artifacts = runtime
    job_id = _start(rt, artifacts, 'print("just a line")')
    time.sleep(0.2)
    result = _submit(rt, artifacts, "r1", "job_read", {"job_id": job_id})
    assert result["stdout"] == "just a line\n"
    assert result["stdout_dropped_bytes"] == 0
