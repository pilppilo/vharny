"""Focused tests for the opt-in Vharness Next alpha application."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    OperatorQuestion,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.cli import main
from vharness.agent.environments import SoftwareEvaluator, SoftwareFixture
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import SessionStatus
from vharness.agent.replay import replay


def _run(tmp_path: Path, *extra: str) -> tuple[int, str, str]:
    output = StringIO()
    error = StringIO()
    code = main(
        [
            "run",
            "--objective",
            "keep value equals 1",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            *extra,
        ],
        output=output,
        error=error,
    )
    return code, output.getvalue(), error.getvalue()


def test_local_smoke_streams_successful_vertical_slice(tmp_path: Path) -> None:
    code, output, error = _run(tmp_path, "--local-smoke")

    assert code == 0
    assert not error
    assert "OBJECTIVE keep value equals 1" in output
    assert "SESSION session-" in output
    assert "OBSERVATION" in output
    assert "main.py" in output
    assert "MODEL decision=ActionChoice" in output
    assert 'ACTION read arguments={"path":"main.py"}' in output
    assert "RUNTIME receipt status=succeeded" in output
    assert "EVALUATION disposition=accepted accepted=True" in output
    assert "BUDGET actions=3/6 model_calls=4/8 evaluations=1/2" in output
    assert "TERMINAL success; external evaluator accepted the task" in output


def test_live_provider_configuration_error_is_actionable(tmp_path: Path) -> None:
    code, output, error = _run(tmp_path)

    assert code == 2
    assert not output
    assert "ERROR provider configuration: no endpoint" in error


def test_preflight_reports_context_and_effective_provider_without_calling_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    output = StringIO()
    error = StringIO()

    code = main(
        [
            "preflight",
            "--objective",
            "inspect the workspace",
            "--workspace",
            str(workspace),
            "--base-url",
            "https://user:secret@example.test/v1?token=hidden",
            "--model",
            "preflight-model",
            "--api-key-env",
            "TEST_PROVIDER_KEY",
        ],
        output=output,
        error=error,
    )

    report = json.loads(output.getvalue())
    assert code == 1  # settings and context fit; provider key is not configured
    assert not error.getvalue()
    assert report["provider"]["base_url"] == "https://example.test/v1"
    assert report["provider"]["model"] == "preflight-model"
    assert report["provider"]["api_key_available"] is False
    assert report["provider"]["connectivity_checked"] is False
    assert report["provider_call_made"] is False
    assert report["context"]["fits"] is True
    assert report["context"]["reserved_output_tokens"] == 512
    assert "secret" not in output.getvalue()
    assert "hidden" not in output.getvalue()


def test_preflight_returns_failure_when_context_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "dummy")
    output = StringIO()
    error = StringIO()

    code = main(
        [
            "preflight",
            "--objective",
            "inspect the workspace",
            "--workspace",
            str(workspace),
            "--base-url",
            "https://example.test/v1",
            "--model",
            "preflight-model",
            "--api-key-env",
            "TEST_PROVIDER_KEY",
            "--token-budget",
            "1",
        ],
        output=output,
        error=error,
    )

    report = json.loads(output.getvalue())
    assert code == 1
    assert report["context"]["fits"] is False
    assert not error.getvalue()


def test_budget_failure_is_terminal_and_nonzero(tmp_path: Path) -> None:
    code, output, error = _run(tmp_path, "--local-smoke", "--max-actions", "1")

    assert code == 1
    assert "BUDGET actions=1/1" in output
    assert "TERMINAL failure; inspect the journal" in output
    assert "action budget exhausted" in error


def test_gymnasium_without_scripted_checkpoint_is_rejected(tmp_path: Path) -> None:
    code, output, error = _run(tmp_path, "--environment", "gymnasium")
    assert code == 2
    assert not output
    assert "scripted-checkpoint" in error


def test_scripted_gymnasium_checkpoint_completes(tmp_path: Path) -> None:
    pytest.importorskip("gymnasium")
    code, output, error = _run(
        tmp_path,
        "--environment",
        "gymnasium",
        "--scripted-checkpoint",
        "--objective",
        "reach the FrozenLake goal",
        "--session-id",
        "checkpoint-test",
        "--max-actions",
        "8",
        "--max-model-calls",
        "10",
        "--max-steps",
        "16",
    )
    assert code == 0, output + error
    assert not error
    assert "scripted gymnasium checkpoint" in output
    assert "TERMINAL success" in output
    journal_path = tmp_path / "session.sqlite3"
    assert journal_path.is_file()
    assert not Path(f"{journal_path}-wal").exists()
    manifest = tmp_path / "manifest.json"
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["journal"] == "session.sqlite3"
    assert payload["artifacts"] == "artifacts"
    assert payload["env_id"] == "FrozenLake-v1"
    assert payload["status"] == "completed"
    assert payload["runtime"]["component_id"] == "gymnasium-official-runtime"
    assert payload["evaluator"]["component_id"] == "gymnasium-official-evaluator"
    assert isinstance(payload["git_dirty"], bool)
    pointer = (tmp_path / "artifacts" / ".journals").read_text(encoding="utf-8").strip()
    assert not Path(pointer).is_absolute()
    store = ArtifactStore(tmp_path / "artifacts")
    assert journal_path.resolve() in store.registered_journals()
    journal = SqliteJournal(journal_path)
    try:
        session_id = payload["session_id"]
        events = journal.events(session_id, limit=max(journal.cursor(session_id), 1))
        assert events
        assert replay(events).status is SessionStatus.COMPLETED
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--max-actions", "7"),
        ("--max-model-calls", "9"),
        ("--max-evaluations", "1"),
        ("--max-steps", "15"),
    ),
)
def test_scripted_checkpoint_rejects_insufficient_explicit_budget(
    tmp_path: Path, option: str, value: str
) -> None:
    code, output, error = _run(
        tmp_path,
        "--environment",
        "gymnasium",
        "--scripted-checkpoint",
        option,
        value,
    )
    assert code == 2
    assert not output
    assert "insufficient budgets" in error
    assert option.removeprefix("--") in error


def test_scripted_checkpoint_rejects_nonzero_seed(tmp_path: Path) -> None:
    code, output, error = _run(
        tmp_path,
        "--environment",
        "gymnasium",
        "--scripted-checkpoint",
        "--gym-seed",
        "1",
    )
    assert code == 2
    assert not output
    assert "seed 0 only" in error


def test_scripted_checkpoint_rejects_non_frozenlake_gym_id(tmp_path: Path) -> None:
    code, output, error = _run(
        tmp_path,
        "--environment",
        "gymnasium",
        "--scripted-checkpoint",
        "--gym-id",
        "CartPole-v1",
    )
    assert code == 2
    assert not output
    assert "FrozenLake-v1 only" in error


def test_provider_key_value_is_not_streamed_or_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "alpha-test-secret-should-never-appear"
    monkeypatch.setenv("ALPHA_TEST_KEY", secret)
    code, output, error = _run(
        tmp_path,
        "--local-smoke",
        "--api-key-env",
        "ALPHA_TEST_KEY",
    )

    assert code == 0
    assert secret not in output + error
    persisted = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert secret.encode() not in persisted


def _seed_software_question(tmp_path: Path) -> None:
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    fixture = SoftwareFixture(artifacts)
    session = Session.create(
        "session-cli",
        TaskSpec(
            "task-1",
            "keep value equals 1",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=6, evaluations=2, model_calls=8),
            fixture.environment_id,
            "software-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
        initial_observation=fixture.initial_observation("session-cli"),
    )
    session.step(ScriptedModel((OperatorQuestion("need a hand?"),)))
    journal.close()


def test_status_and_message_answer_a_stored_question(tmp_path: Path) -> None:
    _seed_software_question(tmp_path)
    output = StringIO()
    error = StringIO()
    code = main(
        [
            "status",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--session-id",
            "session-cli",
        ],
        output=output,
        error=error,
    )
    assert code == 0, error.getvalue()
    assert "need a hand?" in output.getvalue()
    assert "WAIT operator answer" in output.getvalue()
    output = StringIO()
    code = main(
        [
            "message",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--session-id",
            "session-cli",
            "--text",
            "continue",
        ],
        output=output,
        error=error,
    )
    assert code == 0, error.getvalue()
    assert "STATUS running" in output.getvalue()
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    try:
        events = journal.events(
            "session-cli", limit=max(journal.cursor("session-cli"), 1)
        )
        assert replay(events).status is SessionStatus.RUNNING
    finally:
        journal.close()


def test_status_reopens_with_durable_creation_state_after_workspace_change(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "session.sqlite3"
    artifact_path = tmp_path / "artifacts"
    journal = SqliteJournal(journal_path)
    artifacts = ArtifactStore(artifact_path)
    fixture = SoftwareFixture(artifacts)
    session_id = "session-bound-creation-state"
    observation = fixture.initial_observation(session_id)
    Session.create(
        session_id,
        TaskSpec(
            "task-bound-creation-state",
            "keep value equals 1",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=6, evaluations=2, model_calls=8),
            fixture.environment_id,
            "software-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
        initial_observation=observation,
    )
    fixture.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )
    journal.close()

    output = StringIO()
    error = StringIO()
    code = main(
        [
            "status",
            "--journal",
            str(journal_path),
            "--artifacts",
            str(artifact_path),
            "--session-id",
            session_id,
        ],
        output=output,
        error=error,
    )

    assert code == 0, error.getvalue()
    assert "STATUS created" in output.getvalue()


def test_run_operator_wait_exits_zero_without_tty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vharness.agent.cli._build_model",
        lambda _options: (ScriptedModel((OperatorQuestion("need a hand?"),)), None),
    )
    code, output, error = _run(tmp_path, "--local-smoke")
    assert code == 0, error
    assert "WAITING operator-answer" in output
    assert "need a hand?" in output


def test_cli_created_question_can_be_reopened_and_answered(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vharness.agent.cli._build_model",
        lambda _options: (ScriptedModel((OperatorQuestion("need a hand?"),)), None),
    )
    code, output, error = _run(
        tmp_path, "--local-smoke", "--session-id", "session-cli-created"
    )
    assert code == 0, error
    assert "need a hand?" in output

    status_output = StringIO()
    status_error = StringIO()
    status_code = main(
        [
            "status",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--session-id",
            "session-cli-created",
        ],
        output=status_output,
        error=status_error,
    )
    assert status_code == 0, status_error.getvalue()
    assert "need a hand?" in status_output.getvalue()

    message_output = StringIO()
    message_error = StringIO()
    message_code = main(
        [
            "message",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--session-id",
            "session-cli-created",
            "--text",
            "continue",
        ],
        output=message_output,
        error=message_error,
    )
    assert message_code == 0, message_error.getvalue()
    assert "STATUS running" in message_output.getvalue()


def test_cli_message_rejection_is_observable(tmp_path):
    _seed_software_question(tmp_path)
    output = StringIO()
    error = StringIO()
    code = main(
        [
            "message",
            "--journal",
            str(tmp_path / "session.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--session-id",
            "session-cli",
            "--text",
            "stale answer",
            "--question-id",
            "question-other",
        ],
        output=output,
        error=error,
    )
    assert code == 1
    assert "STATUS waiting" in output.getvalue()
    assert "ERROR message rejected" in error.getvalue()
