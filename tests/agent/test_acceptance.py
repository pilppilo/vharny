"""Smoke coverage for an unscripted CompatibleModel on the software fixture.

This is deterministic policy smoke, not a live-provider acceptance campaign.
The generator sees only the visible decision JSON and returns one AgentResult.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from pathlib import Path

from vharness.agent import (
    BudgetLimits,
    CompatibleModel,
    CompletionMode,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.environments import SoftwareEvaluator, SoftwareFixture
from vharness.agent.journal import SqliteJournal
from vharness.core import VERSION

_REPO = Path(__file__).resolve().parents[2]


@dataclass
class VisibleGeneration:
    text: str
    model: str = "acceptance-policy-v1"
    finish_reason: str = "stop"
    error: str | None = None
    prompt_tokens: int = 8
    completion_tokens: int = 12
    latency: float = 0.0


class ObservationPolicyGenerator:
    """Choose the next software-fixture act from the visible request only."""

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, system: str, prompt: str) -> VisibleGeneration:
        del system
        self.calls += 1
        self.prompts.append(prompt)
        request = json.loads(prompt)
        return VisibleGeneration(json.dumps(_choose(request), sort_keys=True))


def _choose(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        return _read()
    observation = request.get("observation")
    receipt = request.get("latest_receipt")
    content = observation.get("content") if isinstance(observation, dict) else None
    status = ""
    if isinstance(receipt, dict) and isinstance(receipt.get("status"), str):
        status = receipt["status"]
    elif isinstance(observation, dict) and isinstance(observation.get("status"), str):
        status = observation["status"]
    if isinstance(content, dict) and content.get("check_passed") is True:
        return {"kind": "EvaluationChoice", "rationale": "native check passed"}
    if isinstance(content, dict) and content.get("check_passed") is False:
        return _write()
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        source = content["content"]
        if "value = 1" in source:
            return _test()
        return _write()
    if status == "succeeded":
        return _test()
    if status == "failed":
        return _write()
    return _read()


def _read() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "read",
        "rationale": "inspect workspace from the published schema",
        "arguments": {"path": "main.py"},
    }


def _write() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "write",
        "rationale": "satisfy the stated objective keep value equals 1",
        "arguments": {"path": "main.py", "content": "value = 1\n"},
    }


def _test() -> dict[str, object]:
    return {
        "kind": "ActionChoice",
        "action_name": "test",
        "rationale": "request the fixture native check",
        "arguments": {},
    }


def _git_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO,
        text=True,
    ).strip()


def test_unscripted_policy_smoke_solves_software_fixture(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    fixture = SoftwareFixture(artifacts)
    session = Session.create(
        "session-accept",
        TaskSpec(
            task_id="task-accept",
            objective="keep value equals 1",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=6, evaluations=2, model_calls=8),
            environment_id=fixture.environment_id,
            evaluation_contract_id="software-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
    )
    generator = ObservationPolicyGenerator()
    model = CompatibleModel(generator, model_id="acceptance-policy-v1", repair_budget=0)
    for _ in range(8):
        if session.view().status is SessionStatus.COMPLETED:
            break
        session.step(model)

    assert session.view().status is SessionStatus.COMPLETED
    assert len(session.accepted_lineage()) == 2
    events = session.events(limit=200)
    kinds = [event.kind for event in events]
    assert "action_intended" in kinds
    assert kinds.count("receipt_received") >= 3
    assert "evaluation_received" in kinds
    accepted = [
        event
        for event in events
        if event.kind == "evaluation_received"
        and event.payload.get("disposition") == "accepted"
    ]
    assert accepted
    assert accepted[0].payload["receipt"]["accepted"] is True
    decided = [event for event in events if event.kind == "model_decided"]
    assert decided
    first = json.loads(str(decided[0].payload["input"]))
    assert "argument_schema" in first["actions"][0]
    assert "result_schemas" in first
    assert "remaining_budgets" in first
    assert first["remaining_budgets"]["actions"]["limit"] == 6
    assert first["remaining_budgets"]["model_calls"]["limit"] == 8
    assert first["model_id"] == "acceptance-policy-v1"
    assert all(event.payload.get("attempts") for event in decided)
    assert all(event.payload.get("usage") for event in decided)
    assert all("manifest" in event.payload for event in decided)
    run = session.manifest()
    assert run.package_version == VERSION
    assert len(run.configuration_digest) == 64
    assert run.environment_id == fixture.environment_id
    counters = session.counters()
    assert counters.model_calls == generator.calls == model.calls
    assert counters.actions >= 3
    assert counters.evaluations >= 1
    assert _git_revision()
    assert "ScriptedModel" not in type(model).__name__
    journal.close()
