"""Compatible model adapter and bounded malformed-output repair."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

import pytest

from vharness.agent import (
    BudgetLimits,
    CompatibleModel,
    CompletionMode,
    LineageQuery,
    Session,
    ScriptedModel,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.context import (
    SYSTEM_PROMPT,
    count_tokens,
    estimate_serialized_tokens,
)
from vharness.agent.errors import ContractError, IntegrityError, TransitionError
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import (
    ModelAttempt,
    ModelCallError,
    ModelResponse,
    configuration_for,
    parse_agent_result,
)
from vharness.agent.replay import replay
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    StateRef,
    UsageMeasurement,
)
from vharness.agent.supervision import (
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)


@dataclass
class FakeGeneration:
    text: str
    model: str = "fake-llm"
    finish_reason: str = "stop"
    error: str | None = None
    prompt_tokens: int | None = 3
    completion_tokens: int | None = 5
    reasoning_tokens: int | None = None


class ScriptedGenerator:
    def __init__(self, texts: tuple[str, ...]) -> None:
        self._texts = list(texts)
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, system: str, prompt: str) -> FakeGeneration:
        del system
        self.calls += 1
        self.prompts.append(prompt)
        if not self._texts:
            return FakeGeneration("", error="exhausted")
        return FakeGeneration(self._texts.pop(0))


class SizedScriptedGenerator(ScriptedGenerator):
    """Scripted text source exposing an explicit output-token reservation."""

    def __init__(self, texts: tuple[str, ...], max_tokens: object) -> None:
        super().__init__(texts)
        self.max_tokens = max_tokens


class FakeEnvironment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FakeRuntime:
    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", "after", digest="1" * 64),
        )

    def reconcile(self, operation_id):
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class FakeEvaluator:
    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=True,
            comparison="improved",
        )

    def reconcile(self, request_id):
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


def test_parse_agent_result_reads_kind_and_tuple_fields():
    result = parse_agent_result('{"kind": "LineageQuery", "node_id": "root-1"}')
    assert isinstance(result, LineageQuery)
    assert result.node_id == "root-1"


def test_system_prompt_names_concrete_result_kinds():
    assert "ActionChoice" in SYSTEM_PROMPT
    assert "EvaluationChoice" in SYSTEM_PROMPT
    assert "naming an AgentResult" not in SYSTEM_PROMPT
    with pytest.raises(ContractError, match="unsupported agent result kind"):
        parse_agent_result('{"kind": "AgentResult"}')


@pytest.mark.parametrize(
    "payload",
    [
        '{"kind": "ActionChoice", "action_name": 1, "rationale": "inspect"}',
        '{"kind": "MemoryUpdate", "item_id": "h", "statement": "s", '
        '"scope": "task", "status": "hypothesis", "evidence_ids": "not-array"}',
    ],
)
def test_typed_result_decoder_rejects_invalid_json_types(payload):
    with pytest.raises(ContractError, match="invalid JSON type"):
        parse_agent_result(payload)


def test_system_prompt_describes_finite_completion_lifecycle_without_domain_text():
    assert "EvaluationChoice requests external evaluation of the current attempt." in (
        SYSTEM_PROMPT
    )
    assert (
        "For a finite task, CompletionProposal is valid only when applicable "
        "current external completion evidence already exists."
    ) in SYSTEM_PROMPT
    assert (
        "If current observations or action receipts indicate readiness but "
        "applicable current external evidence is absent, request "
        "EvaluationChoice."
    ) in SYSTEM_PROMPT
    assert (
        "Do not repeat a CompletionProposal rejected for `no current external "
        "evidence`."
    ) in SYSTEM_PROMPT
    for domain_text in ("FrozenLake", "Gymnasium", "LongCat", "main.py"):
        assert domain_text not in SYSTEM_PROMPT


def test_system_prompt_digest_is_stable_and_content_addressed():
    first = configuration_for(ScriptedModel((LineageQuery(),)), token_budget=16384)
    second = configuration_for(ScriptedModel((LineageQuery(),)), token_budget=16384)

    expected = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert first.system_prompt_digest == expected
    assert second.system_prompt_digest == expected
    assert first == second


def test_compatible_model_repairs_malformed_output_and_counts_both_calls(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = ScriptedGenerator(
        (
            "not json",
            '{"kind": "LineageQuery"}',
        )
    )
    session = Session.create(
        "session-model",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(generator, repair_budget=1)
    assert model.reserved_output_tokens == 256
    assert model.output_settings["reserved_output_tokens"] == 256
    session.step(model)
    assert generator.calls == 2
    assert model.calls == 2
    decided = [
        event for event in session.events(limit=100) if event.kind == "model_decided"
    ]
    assert len(decided) == 1
    assert decided[0].payload["usage"]["model_calls"] == 2
    assert decided[0].payload["usage"]["input_tokens"] == 6
    assert decided[0].payload["usage"]["output_tokens"] == 10
    prompt = generator.prompts[0]
    assert "argument_schema" in prompt
    assert "result_schemas" in prompt
    assert "remaining_budgets" in prompt
    assert "request_id" in prompt
    assert "policy_version" in prompt
    assert "token_counter" in prompt
    assert decided[0].payload["request_id"]
    assert decided[0].payload["input"] == prompt
    assert decided[0].payload["policy_version"] == 2
    assert "included" in decided[0].payload["manifest"]
    assert session.counters().model_calls == 2
    kinds = [event.kind for event in session.events(limit=100)]
    assert kinds.count("model_intended") == 1
    intended = [
        event for event in session.events(limit=100) if event.kind == "model_intended"
    ]
    assert intended[0].payload["input"] == prompt
    assert len(decided[0].payload["attempts"]) == 2
    assert decided[0].payload["attempts"][0]["output"] == "not json"
    assert decided[0].payload["duration_ms"] is not None
    assert (
        decided[0]
        .payload["usage"]["measurement_id"]
        .startswith(decided[0].payload["request_id"])
    )
    journal.close()


class _ExhaustedGenerator:
    """Returns empty, length-limited decodes from a scripted sequence."""

    def __init__(
        self, responses: tuple[FakeGeneration, ...], max_tokens: int = 256
    ) -> None:
        self._responses = list(responses)
        self.max_tokens = max_tokens
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, system: str, prompt: str) -> FakeGeneration:
        del system
        self.calls += 1
        self.prompts.append(prompt)
        return self._responses.pop(0)


def _exhausted() -> FakeGeneration:
    return FakeGeneration(
        "", finish_reason="length", completion_tokens=256, reasoning_tokens=256
    )


def _step_session(tmp_path, model: CompatibleModel) -> tuple[Session, SqliteJournal]:
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    session = Session.create(
        "session-output-ceiling",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    session.step(model)
    return session, journal


def test_empty_decision_is_re_asked_once_at_the_same_ceiling(tmp_path):
    generator = _ExhaustedGenerator(
        (_exhausted(), FakeGeneration('{"kind": "LineageQuery"}'))
    )
    session, journal = _step_session(
        tmp_path, CompatibleModel(generator, repair_budget=1)
    )

    assert generator.calls == 2
    assert "emit it now" in generator.prompts[1]
    assert "reasoning_tokens=256" in generator.prompts[1]
    assert "finish_reason=length" in generator.prompts[1]
    events = session.events(limit=100)
    decided = [event for event in events if event.kind == "model_decided"]
    assert len(decided) == 1
    assert len(decided[0].payload["attempts"]) == 2
    assert decided[0].payload["attempts"][0]["reasoning_tokens"] == 256
    repaired = [event for event in events if event.kind == "model_repaired"]
    assert len(repaired) == 1
    assert repaired[0].payload["failure_class"] == "output-exhausted"
    assert repaired[0].payload["field"] == "output-ceiling"
    assert repaired[0].payload["result_kind"] == "none"
    replay(events)
    journal.close()


def test_second_exhaustion_stays_terminal(tmp_path):
    generator = _ExhaustedGenerator((_exhausted(), _exhausted()))
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    session = Session.create(
        "session-output-ceiling-twice",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )

    with pytest.raises(ModelCallError) as raised:
        session.step(CompatibleModel(generator, repair_budget=1))

    assert raised.value.failure_class == "output-exhausted"
    assert generator.calls == 2
    journal.close()


def test_no_re_ask_when_the_repair_budget_is_spent(tmp_path):
    generator = _ExhaustedGenerator((_exhausted(),))
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    session = Session.create(
        "session-output-ceiling-budget",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )

    with pytest.raises(ModelCallError):
        session.step(CompatibleModel(generator, repair_budget=0))

    assert generator.calls == 1
    journal.close()


def test_main_model_prompt_does_not_treat_guidance_as_completion_evidence():
    assert "Supervisor guidance is unverified advice" in SYSTEM_PROMPT
    assert "corresponding current evidence is visible" in SYSTEM_PROMPT


def test_next_model_request_includes_completion_rejection_feedback(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = ScriptedGenerator(
        (
            '{"kind": "CompletionProposal", "rationale": "private model rationale"}',
            '{"kind": "LineageQuery"}',
        )
    )
    session = Session.create(
        "session-rejection-feedback",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(generator, repair_budget=0)

    session.step(model)
    session.step(model)

    next_prompt = generator.prompts[1]
    assert "completion_rejected reason=no current external evidence" in next_prompt
    assert "EvaluationChoice" in next_prompt
    assert "private model rationale" not in next_prompt
    journal.close()


def test_model_configuration_is_bound_and_reopen_rejects_substitution(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    session = Session.create(
        "session-binding",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    first = ScriptedModel((LineageQuery(),), model_id="provider-model-a")
    session.step(first)
    events = session.events(limit=100)
    bound = next(event for event in events if event.kind == "model_configuration_bound")
    intended = next(event for event in events if event.kind == "model_intended")
    assert bound.sequence < intended.sequence
    assert bound.payload["configuration"]["model_id"] == "provider-model-a"
    with pytest.raises(ContractError, match="model configuration"):
        session.step(ScriptedModel((LineageQuery(),), model_id="provider-model-b"))

    reopened = Session.open(
        "session-binding",
        journal=journal,
        artifacts=artifacts,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    with pytest.raises(ContractError, match="model configuration"):
        reopened.step(ScriptedModel((LineageQuery(),), model_id="provider-model-b"))
    with pytest.raises(ContractError, match="model configuration"):
        reopened.step(first, token_budget=4095)
    journal.close()


def test_manifest_records_exact_serialized_window_accounting(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    session = Session.create(
        "session-token-accounting",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    session.step(ScriptedModel((LineageQuery(),)))
    decided = next(
        event for event in session.events(limit=100) if event.kind == "model_decided"
    )
    manifest = decided.payload["manifest"]
    allocation = manifest["allocation"]
    assert manifest["serialized_input_tokens"] == count_tokens(decided.payload["input"])
    assert manifest["system_prompt_tokens"] == count_tokens(SYSTEM_PROMPT)
    assert manifest["estimated_input_tokens"] == estimate_serialized_tokens(
        decided.payload["input"]
    )
    assert manifest["token_estimator"] == "conservative-chars-v1"
    assert manifest["reserved_output_tokens"] == 256
    assert allocation["unused"] == manifest["token_budget"] - (
        manifest["serialized_input_tokens"]
        + manifest["system_prompt_tokens"]
        + manifest["reserved_output_tokens"]
    )
    assert allocation["unused"] >= 0
    journal.close()


def test_calibrated_projection_refuses_an_over_budget_request(tmp_path):
    session, journal = _model_session(tmp_path)

    class OverBudgetModel:
        model_id = "over-budget"

        def decide(self, request):
            output = '{"kind": "LineageQuery"}'
            return ModelResponse(
                result=LineageQuery(),
                output=output,
                model_id=self.model_id,
                finish_reason="stop",
                usage=UsageMeasurement(
                    measurement_id=f"{request.request_id}-usage",
                    model_calls=1,
                    input_tokens=20000,
                ),
                attempts=(
                    ModelAttempt(
                        output=output,
                        finish_reason="stop",
                        prompt_tokens=20000,
                    ),
                ),
            )

    model = OverBudgetModel()
    session.step(model)

    with pytest.raises(TransitionError, match="projected context exceeds token budget"):
        session.prepare_decision(model)

    overflow = [
        event for event in session.events(limit=200) if event.kind == "context_overflow"
    ]
    assert len(overflow) == 1
    journal.close()


def test_provider_usage_above_the_budget_is_journaled_and_replays(tmp_path):
    session, journal = _model_session(tmp_path)

    class OverBudgetModel:
        model_id = "over-budget"

        def decide(self, request):
            output = '{"kind": "LineageQuery"}'
            return ModelResponse(
                result=LineageQuery(),
                output=output,
                model_id=self.model_id,
                finish_reason="stop",
                usage=UsageMeasurement(
                    measurement_id=f"{request.request_id}-usage",
                    model_calls=1,
                    input_tokens=20000,
                ),
                attempts=(
                    ModelAttempt(
                        output=output,
                        finish_reason="stop",
                        prompt_tokens=20000,
                    ),
                ),
            )

    session.step(OverBudgetModel())

    events = list(session.events(limit=200))
    breaches = [event for event in events if event.kind == "token_budget_breach"]
    assert len(breaches) == 1
    payload = breaches[0].payload
    assert payload["request_id"]
    assert payload["provider_input_tokens"] == 20000
    assert payload["reserved_output_tokens"] == 256
    assert payload["token_budget"] == 16384
    assert payload["estimated_input_tokens"] > 0
    replay(events)
    journal.close()


def test_configured_model_output_reservation_reaches_manifest_and_allocation(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = SizedScriptedGenerator(('{"kind": "LineageQuery"}',), max_tokens=1024)
    session = Session.create(
        "session-configured-output",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(generator, repair_budget=0)

    session.step(model, token_budget=8192)

    bound = next(
        event
        for event in session.events(limit=100)
        if event.kind == "model_configuration_bound"
    )
    decided = next(
        event for event in session.events(limit=100) if event.kind == "model_decided"
    )
    configuration = bound.payload["configuration"]
    manifest = decided.payload["manifest"]
    allocation = manifest["allocation"]
    assert model.reserved_output_tokens == 1024
    assert configuration["output_settings"]["reserved_output_tokens"] == 1024
    assert manifest["reserved_output_tokens"] == 1024
    assert allocation["reserved_output"] == 1024
    assert allocation["unused"] == 8192 - (
        manifest["serialized_input_tokens"] + manifest["system_prompt_tokens"] + 1024
    )
    assert allocation["unused"] >= 0
    journal.close()


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.0, "1024"])
def test_compatible_model_rejects_invalid_output_reservation(invalid):
    with pytest.raises(ContractError, match="reserved output tokens"):
        CompatibleModel(SizedScriptedGenerator(("{}",), invalid), repair_budget=0)


def test_compatible_model_rejects_ambiguous_output_reservation():
    with pytest.raises(ContractError, match="match generator max_tokens"):
        CompatibleModel(
            SizedScriptedGenerator(('{"kind": "LineageQuery"}',), 1024),
            repair_budget=0,
            reserved_output_tokens=256,
        )


def test_compatible_model_accepts_matching_explicit_output_reservation():
    model = CompatibleModel(
        SizedScriptedGenerator(('{"kind": "LineageQuery"}',), 1024),
        repair_budget=0,
        reserved_output_tokens=1024,
    )

    assert model.reserved_output_tokens == 1024


def test_exhausted_repair_is_journaled_and_does_not_dispatch(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = ScriptedGenerator(("nope", "still nope"))
    session = Session.create(
        "session-model",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(generator, repair_budget=1)
    with pytest.raises(ContractError, match="malformed"):
        session.step(model)
    kinds = [event.kind for event in session.events(limit=100)]
    assert "model_malformed" in kinds
    assert "model_intended" in kinds
    assert "action_intended" not in kinds
    assert generator.calls == 2
    assert session.counters().model_calls == 2
    malformed = [
        event for event in session.events(limit=100) if event.kind == "model_malformed"
    ]
    assert malformed[0].payload["usage"]["model_calls"] == 2
    assert malformed[0].payload["input"]
    assert malformed[0].payload["output"] == "still nope"
    assert len(malformed[0].payload["attempts"]) == 2
    journal.close()


@pytest.mark.parametrize(
    ("text", "finish_reason"),
    (("", "stop"), ('{"kind":"ActionChoice"', "length")),
)
def test_provider_output_exhaustion_is_re_asked_once_then_terminal(
    tmp_path, text, finish_reason
):
    journal = SqliteJournal(tmp_path / "session.sqlite3")

    class ExhaustedGenerator:
        calls = 0

        def generate(self, system: str, prompt: str) -> FakeGeneration:
            del system, prompt
            self.calls += 1
            return FakeGeneration(text, finish_reason=finish_reason)

    generator = ExhaustedGenerator()
    session = Session.create(
        "session-output-exhausted",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )

    with pytest.raises(ContractError, match="output exhausted"):
        session.step(CompatibleModel(generator, repair_budget=1))

    # ADR-0017: one bounded re-ask at the same reserved ceiling, then terminal.
    assert generator.calls == 2
    assert session.counters().model_calls == 2
    events = session.events(limit=100)
    failure = next(event for event in events if event.kind == "model_malformed")
    assert failure.payload["error"] == "provider output exhausted"
    assert len(failure.payload["attempts"]) == 2
    assert all(
        attempt["finish_reason"] == finish_reason
        for attempt in failure.payload["attempts"]
    )
    repaired = [event for event in events if event.kind == "model_repaired"]
    assert len(repaired) == 1
    assert repaired[0].payload["failure_class"] == "output-exhausted"
    assert repaired[0].payload["field"] == "output-ceiling"
    replay(events)
    journal.close()


def test_exhausted_action_and_evaluation_budgets_skip_model_and_supervisor(
    tmp_path,
):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = ScriptedGenerator(('{"kind": "LineageQuery"}',))
    supervisor = ScriptedSupervisor(())
    session = Session.create(
        "session-no-dispatch-budget",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=0, evaluations=0, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        supervision=SupervisionCapability(
            supervisor,
            MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
        ),
    )

    with pytest.raises(
        TransitionError, match="action and evaluation budgets exhausted"
    ):
        session.prepare_decision(CompatibleModel(generator, repair_budget=0))

    assert generator.calls == 0
    assert supervisor.calls == 0
    kinds = [event.kind for event in session.events(limit=100)]
    assert "supervisor_intended" not in kinds
    assert "model_intended" not in kinds
    journal.close()


def test_model_call_budget_reserves_repair_bound_before_dispatch(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    generator = ScriptedGenerator(('{"kind": "LineageQuery"}',))
    session = Session.create(
        "session-model",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=1),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(generator, repair_budget=1)
    with pytest.raises(TransitionError, match="model call budget exhausted"):
        session.step(model)
    assert generator.calls == 0
    assert session.counters().model_calls == 0
    journal.close()


def test_failed_transport_is_reserved_measured_and_counted(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")

    class FailingGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, system: str, prompt: str) -> FakeGeneration:
            del system, prompt
            self.calls += 1
            return FakeGeneration(
                "", error="boom", prompt_tokens=4, completion_tokens=0
            )

    failing = FailingGenerator()
    session = Session.create(
        "session-model",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=4),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    model = CompatibleModel(failing, repair_budget=0)
    with pytest.raises(ContractError, match="transport"):
        session.step(model)
    assert failing.calls == 1
    assert session.counters().model_calls == 1
    malformed = [
        event for event in session.events(limit=100) if event.kind == "model_malformed"
    ]
    assert malformed[0].payload["usage"]["model_calls"] == 1
    journal.close()


def test_model_usage_keeps_unknown_repair_tokens_unknown(tmp_path):
    class PartiallyMeasuredGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, system: str, prompt: str) -> FakeGeneration:
            del system, prompt
            self.calls += 1
            if self.calls == 1:
                return FakeGeneration("not json", prompt_tokens=7, completion_tokens=3)
            return FakeGeneration(
                "",
                error="timeout",
                prompt_tokens=None,
                completion_tokens=None,
            )

    generator = PartiallyMeasuredGenerator()
    session, journal = _model_session(tmp_path)
    model = CompatibleModel(generator, repair_budget=1)

    with pytest.raises(ContractError, match="transport"):
        session.step(model)

    malformed = [
        event for event in session.events(limit=100) if event.kind == "model_malformed"
    ]
    assert len(malformed) == 1
    usage = malformed[0].payload["usage"]
    assert usage["model_calls"] == 2
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["duration_ms"] is not None
    attempts = malformed[0].payload["attempts"]
    assert attempts[0]["prompt_tokens"] == 7
    assert attempts[0]["completion_tokens"] == 3
    assert attempts[1]["prompt_tokens"] is None
    assert attempts[1]["completion_tokens"] is None
    counters = session.counters()
    assert counters.model_calls == 2
    assert counters.input_tokens is None
    assert counters.output_tokens is None
    assert len(session._state.ledger.unknown_usage_operation_ids) == 1
    journal.close()


def _model_session(tmp_path, *, model_calls: int = 4):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    session = Session.create(
        "session-model",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=2, evaluations=1, model_calls=model_calls),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    return session, journal


def test_compatible_model_disables_hidden_provider_retries(tmp_path):
    session, journal = _model_session(tmp_path)

    class RetryingGenerator:
        def __init__(self) -> None:
            self.max_retries = 3
            self.length_retries = 1
            self.seen: list[tuple[int, int]] = []

        def generate(self, system: str, prompt: str) -> FakeGeneration:
            del system, prompt
            self.seen.append((self.max_retries, self.length_retries))
            return FakeGeneration('{"kind": "LineageQuery"}')

    generator = RetryingGenerator()
    session.step(CompatibleModel(generator, repair_budget=0))
    assert generator.seen == [(0, 0)]
    assert generator.max_retries == 3
    assert generator.length_retries == 1
    journal.close()


def test_unexpected_model_exception_settles_reservation(tmp_path):
    session, journal = _model_session(tmp_path)

    class BoomModel:
        model_id = "boom"
        repair_budget = 0

        def decide(self, request):
            del request
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="model connector failure"):
        session.step(BoomModel())
    kinds = [event.kind for event in session.events(limit=100)]
    assert "model_intended" in kinds
    assert "model_malformed" in kinds
    from vharness.agent.transitions import interrupted_model_reservations

    assert interrupted_model_reservations(session._state) == ()
    malformed = [
        event for event in session.events(limit=100) if event.kind == "model_malformed"
    ]
    assert malformed[0].payload["error"] == "model connector failure"
    journal.close()


def test_malformed_provider_metadata_is_bounded_before_terminal_persistence(tmp_path):
    class MalformedGenerator:
        def generate(self, system: str, prompt: str):
            del system, prompt
            return FakeGeneration(
                text={"secret": "bearer-token"},
                model={"secret": "configured-key"},
                finish_reason=["unexpected"],
                error={"secret": "provider-key"},
                prompt_tokens=[1],
                completion_tokens=10**100,
            )

    session, journal = _model_session(tmp_path, model_calls=2)
    with pytest.raises(ContractError, match="model transport failed"):
        session.step(CompatibleModel(MalformedGenerator(), repair_budget=0))

    malformed = next(
        event for event in session.events(limit=100) if event.kind == "model_malformed"
    )
    encoded = str(malformed.payload)
    assert "bearer-token" not in encoded
    assert "configured-key" not in encoded
    assert "provider-key" not in encoded
    assert malformed.payload["model_id"] == "compatible"
    assert malformed.payload["attempts"][0]["output"] == ""
    assert malformed.payload["attempts"][0]["error"] == "provider transport failure"
    assert session._state.ledger.active_reservations == ()
    journal.close()


def test_model_terminal_must_match_durable_intent_and_be_unique(tmp_path):
    session, journal = _model_session(tmp_path)
    session.step(ScriptedModel((LineageQuery(),)))
    events = list(session.events(limit=100))
    terminal_index = next(
        index for index, event in enumerate(events) if event.kind == "model_decided"
    )
    terminal = events[terminal_index]
    forged_payload = dict(terminal.payload)
    forged_payload["input"] = "forged request"
    events[terminal_index] = replace(terminal, payload=forged_payload)
    with pytest.raises(IntegrityError, match="terminal input"):
        replay(events)

    for field, value, message in (
        ("model_id", "forged-model", "terminal model_id"),
        ("cursor", terminal.payload["cursor"] + 1, "terminal cursor"),
    ):
        forged_payload = dict(terminal.payload)
        forged_payload[field] = value
        events[terminal_index] = replace(terminal, payload=forged_payload)
        with pytest.raises(IntegrityError, match=message):
            replay(events)
    events[terminal_index] = terminal

    duplicate = replace(
        terminal,
        event_id="duplicate-model-terminal",
        sequence=len(events) + 1,
    )
    with pytest.raises(IntegrityError, match="already recorded"):
        replay([*session.events(limit=100), duplicate])
    journal.close()


def test_open_reconciles_interrupted_model_reservation(tmp_path):
    session, journal = _model_session(tmp_path)
    session.begin_attempt()
    from vharness.agent.models import ResourceReservation
    from vharness.agent.transitions import (
        intend_model_call,
        interrupted_model_reservations,
    )

    reservation = ResourceReservation(
        "reservation-crash",
        "decision-crash",
        1,
        model_calls=1,
    )
    intended = intend_model_call(session._state, reservation)
    session._persist(
        "model_intended",
        intended,
        {
            "reservation": reservation,
            "request_id": "decision-crash",
            "input": '{"objective":"inspect the fixture"}',
        },
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    opened = Session.open(
        "session-model",
        journal=journal,
        artifacts=artifacts,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    kinds = [event.kind for event in opened.events(limit=100)]
    assert kinds.count("model_malformed") == 1
    assert interrupted_model_reservations(opened._state) == ()
    journal.close()
