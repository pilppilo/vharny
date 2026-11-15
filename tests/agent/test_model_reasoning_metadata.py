"""Reasoning-token metadata: extraction, bounding, and the durable model record."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from vharness.agent import (
    BudgetLimits,
    CompatibleModel,
    CompletionMode,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import ModelAttempt, bounded_model_attempts
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    StateRef,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.generators import openai_compat
from vharness.generators.openai_compat import OpenAICompatible

REASONING_TOKENS = 4321


@dataclass
class _Generation:
    text: str
    model: str = "fake-llm"
    finish_reason: str = "stop"
    error: str | None = None
    prompt_tokens: int | None = 3
    completion_tokens: int | None = 5
    reasoning_tokens: int | None = None


class _Generator:
    """Returns one scripted decode and reports a fixed reasoning count."""

    def __init__(self, texts: tuple[str, ...], reasoning_tokens: int) -> None:
        self._texts = list(texts)
        self._reasoning_tokens = reasoning_tokens

    def generate(self, system: str, prompt: str) -> _Generation:
        del system, prompt
        return _Generation(self._texts.pop(0), reasoning_tokens=self._reasoning_tokens)


class _Environment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _Runtime:
    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal) -> ExecutionReceipt:
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", "after", digest="1" * 64),
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _Evaluator:
    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        raise AssertionError("the fixture does not request evaluation")

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


def _action() -> str:
    return (
        '{"kind": "ActionChoice", "action_name": "inspect", '
        '"rationale": "fixture", "arguments": {"command": "ss -lnt"}}'
    )


def _response(reasoning_tokens: int | None) -> SimpleNamespace:
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens)
        if reasoning_tokens is not None
        else None
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"kind": "LineageQuery"}'),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=22,
            completion_tokens_details=details,
        ),
    )


def _generator(monkeypatch: pytest.MonkeyPatch, response: SimpleNamespace):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    monkeypatch.setattr(openai_compat, "OpenAI", lambda **_: client)
    return OpenAICompatible("http://127.0.0.1:1/v1", "fake-model")


def test_adapter_reports_provider_reasoning_tokens(monkeypatch):
    generator = _generator(monkeypatch, _response(17))

    generation = generator.generate("system", "prompt")

    assert generation.reasoning_tokens == 17
    assert generation.text == '{"kind": "LineageQuery"}'


def test_adapter_leaves_reasoning_unknown_when_provider_omits_it(monkeypatch):
    generator = _generator(monkeypatch, _response(None))

    generation = generator.generate("system", "prompt")

    assert generation.reasoning_tokens is None


def test_reasoning_tokens_reach_the_durable_model_record(tmp_path: Path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    session = Session.create(
        "session-reasoning",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=_Environment(),
        runtime=_Runtime(),
        evaluator=_Evaluator(),
    )
    model = CompatibleModel(
        _Generator((_action(),), reasoning_tokens=REASONING_TOKENS),
        repair_budget=0,
    )

    session.step(model)

    decided = [
        event
        for event in journal.events(session.session_id, limit=100)
        if event.kind == "model_decided"
    ]
    assert len(decided) == 1
    attempts = decided[0].payload["attempts"]
    assert attempts[0]["reasoning_tokens"] == REASONING_TOKENS


def test_bounded_attempts_preserve_reasoning_and_drop_invalid_values():
    kept = bounded_model_attempts(
        (ModelAttempt(output="", finish_reason="stop", reasoning_tokens=7),)
    )
    assert kept[0].reasoning_tokens == 7

    dropped = bounded_model_attempts(
        (ModelAttempt(output="", finish_reason="stop", reasoning_tokens=-1),)
    )
    assert dropped[0].reasoning_tokens is None

    boolean = bounded_model_attempts(
        (ModelAttempt(output="", finish_reason="stop", reasoning_tokens=True),)
    )
    assert boolean[0].reasoning_tokens is None


def test_attempt_payload_names_the_reasoning_field():
    encoded = canonical_json(
        ModelAttempt(output="", finish_reason="length", reasoning_tokens=16384)
    )
    assert '"reasoning_tokens":16384' in encoded
