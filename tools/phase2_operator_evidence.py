"""Generate and verify retained PHASE-0002 operator-control evidence.

The bundle is intentionally a deterministic scripted-model control exercise.
It proves journal, projection, CLI, and resume mechanics; it does not claim
autonomous model reasoning or broad task capability.
"""

from __future__ import annotations

# This is a bounded evidence harness rather than a product module. The helpers
# stay explicit so the retained report can be audited against durable events.
# pylint: disable=duplicate-code,too-many-arguments,too-many-branches
# pylint: disable=too-many-locals,too-many-return-statements,too-many-statements

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping, Sequence, cast

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    EvaluationChoice,
    OperatorQuestion,
    PlanUpdate,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json, parse_json_object
from vharness.agent.evidence import counters as derive_counters
from vharness.agent.evidence import manifest as derive_manifest
from vharness.agent.errors import AgentError
from vharness.agent.environments import SoftwareEvaluator, SoftwareFixture
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ArtifactRef,
    ComponentIdentity,
    Event,
    Message,
    Pause,
    Resume,
    RunIdentity,
    SessionView,
    StateRef,
    Stop,
)
from vharness.agent.operator import build_operator_view
from vharness.agent.replay import _legacy_projection_digest, replay
from vharness.agent.session_state import state_ref_from
from vharness.agent.transitions import SessionState

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PRIMARY_ID = "phase2-operator-steering-resume"
STOPPED_ID = "phase2-stopped-question"
BUNDLE_ID = "phase-0002-operator-steering-resume"
MODEL_ID = "phase2-operator-scripted-v1"
TOKEN_BUDGET = 4096
PRIMARY_BUDGETS = {"actions": 3, "model_calls": 6, "evaluations": 1}
STOPPED_BUDGETS = {"actions": 0, "model_calls": 1, "evaluations": 0}
PRIMARY_ACTIONS = ("read", "write", "test")
_CHILD_ENVIRONMENT_KEYS = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP"})
PRIMARY_CLI_CONTEXT = (
    "OBJECTIVE Preserve operator direction while safely completing the fixture",
    "PLAN Read the fixture, apply the requested change, then evaluate.",
    "BUDGET remaining ",
)
OPERATOR_DIRECTION = "Prioritize a read before any write and keep the plan visible."
OPERATOR_ANSWER = "Yes. Continue with the planned write and test after resume."
CONFIGURATION = {
    "evidence": "phase-0002-operator-steering-resume",
    "model": {
        "provider": "ScriptedModel",
        "model_id": MODEL_ID,
        "policy_version": 1,
        "token_counter": "whitespace-v1",
        "token_budget": TOKEN_BUDGET,
        "repair_budget": 0,
        "coordinator_retries": 0,
    },
    "primary_budgets": PRIMARY_BUDGETS,
    "stopped_question_budgets": STOPPED_BUDGETS,
    "task_ids": {
        "primary": "vharness-phase2-operator-steering-resume",
        "stopped": "vharness-phase2-stopped-question",
    },
    "environment": "software-fixture",
    "runtime": "software-fixture-runtime:v1",
    "evaluator": "software-fixture-evaluator:v1",
}


class _ReadOnlyJournal(SqliteJournal):
    """Minimal journal adapter that refuses all writes during verification."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._connection = sqlite3.connect(
            self._path.as_uri() + "?mode=ro&immutable=1", uri=True
        )

    @property
    def path(self) -> Path:
        """Return the read-only journal path for dependency checks."""
        return self._path

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[Event, ...]:
        """Read an ordered event page without opening SQLite in write mode."""
        rows = self._connection.execute(
            """
            SELECT event_id, session_id, sequence, kind, objective_version, payload,
                   recorded_at, causation_id, correlation_id, schema_version
            FROM events
            WHERE session_id = ? AND sequence > ?
            ORDER BY sequence
            LIMIT ?
            """,
            (session_id, after_sequence, limit),
        ).fetchall()
        return tuple(
            Event(
                event_id=row[0],
                session_id=row[1],
                sequence=row[2],
                kind=row[3],
                objective_version=row[4],
                payload=parse_json_object(row[5]),
                recorded_at=row[6],
                causation_id=row[7],
                correlation_id=row[8],
                schema_version=row[9],
            )
            for row in rows
        )

    def append_batch(self, _events: object, *, expected_cursor: int) -> int:
        """Reject any attempted journal mutation by reopened-session code."""
        del expected_cursor
        raise RuntimeError("read-only evidence verification cannot append events")

    def close(self) -> None:
        """Close the read-only SQLite connection without checkpointing."""
        self._connection.close()


class _ReadOnlyArtifactStore(ArtifactStore):
    """ArtifactStore view whose constructor and journal registration are read-only."""

    def __init__(self, path: Path) -> None:
        self._root = path.resolve()
        self._lock = threading.RLock()

    def register_journal(self, _path: str | Path) -> None:
        """Do not mutate `.journals` while reopening retained evidence."""


def main(argv: list[str] | None = None) -> int:
    """Generate a bundle or verify it from a fresh process."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    record = subparsers.add_parser("record-fresh")
    record.add_argument("--bundle", type=Path, required=True)
    record.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    arguments = parser.parse_args(argv)
    if arguments.command == "generate":
        generate_bundle(arguments.output, arguments.source_root)
    elif arguments.command == "record-fresh":
        _record_fresh_process(arguments.bundle, arguments.source_root)
    else:
        verify_bundle(arguments.bundle, arguments.source_root)
    return 0


def generate_bundle(output: Path, source_root: Path) -> None:
    """Create one portable bundle from a clean source snapshot."""
    source = _source_identity(source_root)
    if source["git_dirty"]:
        raise RuntimeError("evidence generation requires a clean source worktree")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"evidence output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    cli_records: list[dict[str, object]] = []
    primary = _generate_primary(output, source_root, cli_records)
    stopped = _generate_stopped(output)
    _normalize_text_file(output / "artifacts" / ".journals")
    _normalize_text_file(output / "stopped-artifacts" / ".journals")
    _write_context_manifests(output, primary, stopped)
    _write_transcript(output, primary, stopped, cli_records)
    reproduce = output / "reproduce.py"
    reproduce.write_bytes(_normalized_bytes(Path(__file__).read_bytes()))
    _run_fresh_process_recorder(output, source_root)
    report = _build_report(output, source, primary, stopped, cli_records)
    _write_json(output / "report.json", report)
    manifest = _build_bundle_manifest(output, source, primary, stopped)
    _write_json(output / "manifest.json", manifest)
    _run_fresh_process_verifier(output, source_root)
    print(canonical_json(manifest))


def verify_bundle(bundle: Path, source_root: Path) -> None:
    """Verify a retained bundle without modifying any file in it."""
    if _is_reparse_point(bundle):
        raise RuntimeError("bundle root is a reparse point")
    bundle = bundle.resolve()
    manifest = _read_json(bundle / "manifest.json")
    _validate_bundle_manifest(bundle, manifest)
    _validate_source_pin(bundle, source_root, manifest)
    report = _read_json(bundle / "report.json")
    report_mapping = _mapping(report)
    if report_mapping.get("evidence_id") != BUNDLE_ID:
        raise RuntimeError("report evidence ID is invalid")
    if report_mapping.get("configuration_digest") != _configuration_digest():
        raise RuntimeError("report configuration digest is invalid")
    _require_equal(
        "report source",
        report_mapping.get("source"),
        _mapping(manifest).get("source"),
    )
    expected_fresh = _read_json(bundle / "fresh-process-reopen.json")
    streams: dict[str, tuple[Event, ...]] = {}
    for name, session_id in (("primary", PRIMARY_ID), ("stopped", STOPPED_ID)):
        journal_path = bundle / _journal_name(name)
        artifacts_path = bundle / _artifact_name(name)
        if not journal_path.is_file():
            raise RuntimeError(f"missing journal for {name}: {journal_path.name}")
        if not artifacts_path.is_dir():
            raise RuntimeError(
                f"missing artifact store for {name}: {artifacts_path.name}"
            )
        journal = _ReadOnlyJournal(journal_path)
        try:
            events = _all_events(journal, session_id)
            streams[name] = events
            journal_facts = _journal_facts(journal_path, events)
            expected_journal = _mapping(_mapping(manifest).get("journals")).get(name)
            _require_equal(
                f"journal facts for {name}",
                journal_facts,
                expected_journal,
            )
            _validate_journal_facts(name, journal_facts)
            artifact_facts = _artifact_facts(artifacts_path, events)
            expected_artifacts = _mapping(_mapping(manifest).get("artifact_stores"))
            _require_equal(
                f"artifact facts for {name}",
                artifact_facts,
                expected_artifacts.get(name),
            )
            _validate_artifact_facts(
                bundle, journal_path, artifacts_path, artifact_facts
            )
        finally:
            journal.close()
    results = _fresh_process_results(bundle)
    actual_fresh = {"runs": results}
    _require_equal("stored fresh-process verification", actual_fresh, expected_fresh)
    _validate_report_against_events(report, streams)
    print(canonical_json(expected_fresh))


def _record_fresh_process(bundle: Path, source_root: Path) -> None:
    """Record fresh-process projections while assembling a new bundle."""
    del source_root
    bundle = bundle.resolve()
    _write_json(
        bundle / "fresh-process-reopen.json",
        {"runs": _fresh_process_results(bundle)},
    )


def _fresh_process_results(bundle: Path) -> list[dict[str, object]]:
    """Collect reopen/replay facts without writing to the bundle."""
    results = []
    for name, session_id in (("primary", PRIMARY_ID), ("stopped", STOPPED_ID)):
        journal_path = bundle / _journal_name(name)
        artifacts_path = bundle / _artifact_name(name)
        if not journal_path.is_file():
            raise RuntimeError(f"missing journal for {name}: {journal_path.name}")
        if not artifacts_path.is_dir():
            raise RuntimeError(
                f"missing artifact store for {name}: {artifacts_path.name}"
            )
        journal = _ReadOnlyJournal(journal_path)
        try:
            events = _all_events(journal, session_id)
            artifacts = _ReadOnlyArtifactStore(artifacts_path)
            environment = SoftwareFixture(
                artifacts, initial_state=_creation_initial_state(events, name)
            )
            evaluator = SoftwareEvaluator(environment, artifacts)
            opened = Session.open(
                session_id,
                journal=journal,
                artifacts=artifacts,
                environment=environment,
                runtime=environment,
                evaluator=evaluator,
                run_identity=_run_identity(),
            )
            replayed = replay(events)
            opened_operator_view = opened.operator_view()
            replay_operator_view = build_operator_view(session_id, replayed, events)
            result = {
                "name": name,
                "session_id": session_id,
                "replay_projection_sha256": _projection_digest(replayed),
                "opened_view": _jsonable(opened.view()),
                "opened_operator_view": _jsonable(opened_operator_view),
                "replay_operator_view": _jsonable(replay_operator_view),
                "replay_status": replayed.status.value,
                "replay_cursor": events[-1].sequence,
                "replay_matches_open_view": _view_matches_state(
                    opened.view(), replayed
                ),
                "operator_view_matches_replay": (
                    opened_operator_view == replay_operator_view
                ),
                "accepted_lineage": [
                    _jsonable(item) for item in replayed.accepted_lineage
                ],
                "counters_match": opened.counters() == derive_counters(events),
                "manifest_match": opened.manifest() == derive_manifest(events),
            }
            if not result["replay_matches_open_view"]:
                raise RuntimeError(f"fresh replay disagrees for {name}")
            if (
                not result["operator_view_matches_replay"]
                or not result["counters_match"]
                or not result["manifest_match"]
            ):
                raise RuntimeError(f"fresh evidence projection disagrees for {name}")
            results.append(result)
        finally:
            journal.close()
    return results


def _generate_primary(
    output: Path, source_root: Path, cli_records: list[dict[str, object]]
) -> dict[str, object]:
    """Exercise plan, CLI steering, question wait, pause, resume, and commit."""
    journal_path = output / "session.sqlite3"
    artifacts_path = output / "artifacts"
    session, journal, _artifacts, _environment = _create_session(
        journal_path,
        artifacts_path,
        PRIMARY_ID,
        PRIMARY_BUDGETS,
    )
    model = ScriptedModel(
        (
            PlanUpdate("Read the fixture, apply the requested change, then evaluate."),
            ActionChoice(
                "read", "inspect before changing the fixture", {"path": "main.py"}
            ),
            OperatorQuestion("May I continue with the planned write and test?"),
            ActionChoice(
                "write",
                "apply the requested value after operator direction",
                {"path": "main.py", "content": "value = 1\n"},
            ),
            ActionChoice("test", "run the fixture check after the write"),
            EvaluationChoice("ask the external fixture evaluator for acceptance"),
        ),
        model_id=MODEL_ID,
    )
    session.step(model, token_budget=TOKEN_BUDGET)
    journal.close()

    cli_records.append(
        _run_cli(
            output,
            source_root,
            "status-before-direction",
            "status",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            expected_returncode=0,
            stdout_contains=("STATUS running", *PRIMARY_CLI_CONTEXT),
        )
    )
    cli_records.append(
        _run_cli(
            output,
            source_root,
            "message-direction",
            "message",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            "--text",
            OPERATOR_DIRECTION,
            expected_returncode=0,
            stdout_contains=(
                "STATUS running",
                *PRIMARY_CLI_CONTEXT,
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
            ),
        )
    )

    session, journal, _artifacts, _environment = _open_session(
        journal_path, artifacts_path, PRIMARY_ID
    )
    session.step(model, token_budget=TOKEN_BUDGET)
    session.step(model, token_budget=TOKEN_BUDGET)
    events = _all_events(journal, PRIMARY_ID)
    question_event = _last_event(events, "question_recorded")
    question_id = _string_field(question_event.payload, "question_id")
    context_event = _context_after_operator_direction(events)
    journal.close()

    cli_records.append(
        _run_cli(
            output,
            source_root,
            "status-question",
            "status",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            expected_returncode=0,
            stdout_contains=(
                "STATUS waiting",
                *PRIMARY_CLI_CONTEXT,
                "WAIT operator answer",
                "QUESTION ",
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
                "STREAM ACTION read ",
                "STREAM RUNTIME succeeded ",
            ),
        )
    )
    cli_records.append(
        _run_cli(
            output,
            source_root,
            "message-wrong-question-id",
            "message",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            "--text",
            "This answer is intentionally rejected.",
            "--question-id",
            "question-not-the-open-one",
            expected_returncode=1,
            stdout_contains=(
                "STATUS waiting",
                *PRIMARY_CLI_CONTEXT,
                "WAIT operator answer",
                "QUESTION ",
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
            ),
            stderr_exact="ERROR message rejected: message does not answer the open question\n",
        )
    )

    session, journal, _artifacts, _environment = _open_session(
        journal_path, artifacts_path, PRIMARY_ID
    )
    before_pause = _event_metrics(_all_events(journal, PRIMARY_ID))
    session.enqueue(Pause("pause-for-operator-evidence", PRIMARY_ID))
    session.advance()
    paused_metrics = _event_metrics(_all_events(journal, PRIMARY_ID))
    calls_before_suppressed_step = model.calls
    if before_pause != paused_metrics:
        raise RuntimeError("pausing changed durable work metrics")
    if calls_before_suppressed_step != before_pause["model_calls"]:
        raise RuntimeError("pause model-call baseline disagrees with the journal")
    session.step(model, token_budget=TOKEN_BUDGET)
    after_suppressed_step = _event_metrics(_all_events(journal, PRIMARY_ID))
    if model.calls != calls_before_suppressed_step:
        raise RuntimeError("paused session invoked the scripted model")
    if after_suppressed_step != paused_metrics:
        raise RuntimeError("paused session changed durable work metrics")
    journal.close()

    cli_records.append(
        _run_cli(
            output,
            source_root,
            "status-paused",
            "status",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            expected_returncode=0,
            stdout_contains=(
                "STATUS paused",
                *PRIMARY_CLI_CONTEXT,
                "WAIT operator answer",
                "QUESTION ",
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
                "STREAM ACTION read ",
                "STREAM RUNTIME succeeded ",
            ),
        )
    )
    cli_records.append(
        _run_cli(
            output,
            source_root,
            "message-answer-while-paused",
            "message",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            "--text",
            OPERATOR_ANSWER,
            "--question-id",
            question_id,
            expected_returncode=0,
            stdout_contains=(
                "STATUS paused",
                *PRIMARY_CLI_CONTEXT,
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
                "STREAM OPERATOR " + OPERATOR_ANSWER,
                "STREAM ACTION read ",
                "STREAM RUNTIME succeeded ",
            ),
            stdout_not_contains=("WAIT operator answer", "QUESTION "),
        )
    )
    cli_records.append(
        _run_cli(
            output,
            source_root,
            "status-answered-still-paused",
            "status",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            expected_returncode=0,
            stdout_contains=(
                "STATUS paused",
                *PRIMARY_CLI_CONTEXT,
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
                "STREAM OPERATOR " + OPERATOR_ANSWER,
            ),
            stdout_not_contains=("WAIT operator answer", "QUESTION "),
        )
    )

    session, journal, _artifacts, _environment = _open_session(
        journal_path, artifacts_path, PRIMARY_ID
    )
    answered_view = session.view()
    if answered_view.status.value != "paused" or answered_view.wait_reason is not None:
        raise RuntimeError("answer implicitly resumed or left the question blocked")
    pending_effect_before_resume = answered_view.pending_proposal_id
    if pending_effect_before_resume is not None:
        raise RuntimeError("resume evidence has an in-flight effect")
    session.enqueue(Resume("resume-after-operator-answer", PRIMARY_ID))
    session.advance()
    resumed_view = session.view()
    if resumed_view.status.value != "running":
        raise RuntimeError("resume did not return the answered session to running")
    resumed_operator_view = session.operator_view()
    if resumed_operator_view.operator_direction != OPERATOR_ANSWER or (
        OPERATOR_DIRECTION not in resumed_operator_view.acknowledged_steering
    ):
        raise RuntimeError("resume lost the durable operator direction")
    session.step(model, token_budget=TOKEN_BUDGET)
    session.step(model, token_budget=TOKEN_BUDGET)
    session.step(model, token_budget=TOKEN_BUDGET)
    checkpoint = session.checkpoint()
    events = _all_events(journal, PRIMARY_ID)
    final_view = session.view()
    if final_view.status.value != "completed":
        raise RuntimeError("primary operator evidence did not complete")
    _assert_action_receipt_sequence(events)
    journal.close()
    cli_records.append(
        _run_cli(
            output,
            source_root,
            "status-final",
            "status",
            "--journal",
            "session.sqlite3",
            "--artifacts",
            "artifacts",
            "--session-id",
            PRIMARY_ID,
            expected_returncode=0,
            stdout_contains=(
                "STATUS completed",
                *PRIMARY_CLI_CONTEXT,
                "STREAM OPERATOR " + OPERATOR_DIRECTION,
                "STREAM OPERATOR " + OPERATOR_ANSWER,
                "STREAM ACTION read ",
                "STREAM ACTION write ",
                "STREAM ACTION test ",
                "STREAM EVALUATION requested",
                "STREAM EVALUATION accepted comparison=improved",
                "STREAM LINEAGE promoted",
            ),
        )
    )
    return {
        "name": "primary",
        "session_id": PRIMARY_ID,
        "journal": "session.sqlite3",
        "artifacts": "artifacts",
        "question_id": question_id,
        "question_sequence": question_event.sequence,
        "context_sequence": context_event.sequence,
        "context_request_id": _string_field(context_event.payload, "request_id"),
        "context_contains_direction": "Prioritize a read before any write"
        in str(context_event.payload.get("input", "")),
        "before_pause_metrics": before_pause,
        "paused_metrics": paused_metrics,
        "after_suppressed_step_metrics": after_suppressed_step,
        "model_calls_at_pause": calls_before_suppressed_step,
        "pending_effect_before_resume": pending_effect_before_resume,
        "answer_sequence": _last_event(events, "question_answered").sequence,
        "pause_sequence": _last_event_before(
            events, "session_paused", checkpoint.cursor + 1
        ).sequence,
        "resume_sequence": _last_event_before(
            events, "session_resumed", checkpoint.cursor + 1
        ).sequence,
        "checkpoint_sequence": checkpoint.cursor + 1,
        "final_status": final_view.status.value,
        "final_lineage_head": final_view.lineage_head.node_id,
        "action_ids": _action_ids(events),
        "action_names": _action_names(events),
        "events": events,
    }


def _generate_stopped(output: Path) -> dict[str, object]:
    """Retain the stopped-question answer behavior as a paired control run."""
    journal_path = output / "stopped-question.sqlite3"
    artifacts_path = output / "stopped-artifacts"
    session, journal, _artifacts, _environment = _create_session(
        journal_path,
        artifacts_path,
        STOPPED_ID,
        STOPPED_BUDGETS,
    )
    model = ScriptedModel(
        (OperatorQuestion("Answering this must not restart a stopped session."),),
        model_id=MODEL_ID,
    )
    session.step(model, token_budget=TOKEN_BUDGET)
    journal.close()
    session, journal, _artifacts, _environment = _open_session(
        journal_path, artifacts_path, STOPPED_ID
    )
    question = _last_event(_all_events(journal, STOPPED_ID), "question_recorded")
    question_id = _string_field(question.payload, "question_id")
    session.enqueue(Stop("stop-before-answer", STOPPED_ID))
    session.advance()
    session.enqueue(
        Message(
            "answer-after-stop",
            STOPPED_ID,
            "The stop remains authoritative after this answer.",
            question_id,
        )
    )
    session.advance()
    if (
        session.view().status.value != "stopped"
        or session.view().wait_reason is not None
    ):
        raise RuntimeError("stopped question answer changed terminal control state")
    events = _all_events(journal, STOPPED_ID)
    journal.close()
    return {
        "name": "stopped",
        "session_id": STOPPED_ID,
        "journal": "stopped-question.sqlite3",
        "artifacts": "stopped-artifacts",
        "question_id": question_id,
        "question_sequence": question.sequence,
        "answer_sequence": _last_event(events, "question_answered").sequence,
        "stop_sequence": _last_event(events, "session_stopped").sequence,
        "model_calls": model.calls,
        "action_ids": _action_ids(events),
        "final_status": "stopped",
        "events": events,
    }


def _create_session(
    journal_path: Path,
    artifacts_path: Path,
    session_id: str,
    budgets: Mapping[str, int],
) -> tuple[Session, SqliteJournal, ArtifactStore, SoftwareFixture]:
    """Create one evidence session with fixed software-fixture identities."""
    journal = SqliteJournal(journal_path)
    artifacts = ArtifactStore(artifacts_path)
    environment = SoftwareFixture(artifacts)
    task_id = (
        "vharness-phase2-operator-steering-resume"
        if session_id == PRIMARY_ID
        else "vharness-phase2-stopped-question"
    )
    task = TaskSpec(
        task_id=task_id,
        objective=(
            "Preserve operator direction while safely completing the fixture"
            if session_id == PRIMARY_ID
            else "Answer a question without restarting a stopped session"
        ),
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(
            actions=budgets["actions"],
            model_calls=budgets["model_calls"],
            evaluations=budgets["evaluations"],
        ),
        environment_id=environment.environment_id,
        evaluation_contract_id="software-fixture-evaluator-v1",
    )
    session = Session.create(
        session_id,
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=environment,
        evaluator=SoftwareEvaluator(environment, artifacts),
        run_identity=_run_identity(),
        initial_observation=environment.initial_observation(session_id),
    )
    return session, journal, artifacts, environment


def _open_session(
    journal_path: Path, artifacts_path: Path, session_id: str
) -> tuple[Session, SqliteJournal, ArtifactStore, SoftwareFixture]:
    """Open one session with a fresh connector set."""
    journal = SqliteJournal(journal_path)
    artifacts = ArtifactStore(artifacts_path)
    try:
        events = _all_events(journal, session_id)
        initial_state = _creation_initial_state(events, session_id)
        environment = SoftwareFixture(artifacts, initial_state=initial_state)
        session = Session.open(
            session_id,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=environment,
            evaluator=SoftwareEvaluator(environment, artifacts),
            run_identity=_run_identity(),
        )
    except (AgentError, RuntimeError, sqlite3.Error, OSError, ValueError):
        journal.close()
        raise
    return session, journal, artifacts, environment


def _creation_initial_state(events: Sequence[Event], label: str) -> StateRef:
    """Decode the software contract state recorded by the creation event."""
    if not events or events[0].kind != "session_created":
        raise RuntimeError(f"session {label!r} has no creation event")
    environment = _mapping(events[0].payload.get("environment"))
    initial_state = environment.get("initial_state")
    if not isinstance(initial_state, Mapping):
        raise RuntimeError(f"session {label!r} lacks environment initial state")
    return state_ref_from(initial_state)


def _run_fresh_process_verifier(output: Path, source_root: Path) -> None:
    """Run the non-mutating verifier in a distinct Python process."""
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "verify",
            "--bundle",
            ".",
            "--source-root",
            str(source_root.resolve()),
        ],
        cwd=output,
        env=_python_environment(source_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "fresh-process verification failed: "
            f"{completed.stdout}\n{completed.stderr}"
        )


def _run_fresh_process_recorder(output: Path, source_root: Path) -> None:
    """Create the stored fresh-process record in a distinct process."""
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "record-fresh",
            "--bundle",
            ".",
            "--source-root",
            str(source_root.resolve()),
        ],
        cwd=output,
        env=_python_environment(source_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "fresh-process recording failed: " f"{completed.stdout}\n{completed.stderr}"
        )


def _run_cli(
    output: Path,
    source_root: Path,
    label: str,
    *arguments: str,
    expected_returncode: int = 0,
    stdout_contains: Sequence[str] = (),
    stdout_not_contains: Sequence[str] = (),
    stderr_exact: str | None = None,
) -> dict[str, object]:
    """Invoke the opt-in CLI, assert its proof surface, and retain output."""
    completed = subprocess.run(
        [sys.executable, "-m", "vharness.agent.cli", *arguments],
        cwd=output,
        env=_python_environment(source_root),
        capture_output=True,
        text=True,
        check=False,
    )
    record = {
        "label": label,
        "argv": ["vharness-agent", *arguments],
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != expected_returncode:
        raise RuntimeError(
            f"CLI {label} returned {completed.returncode}, "
            f"expected {expected_returncode}: {completed.stdout}\n{completed.stderr}"
        )
    for required in stdout_contains:
        if required not in completed.stdout:
            raise RuntimeError(f"CLI {label} omitted stdout proof: {required}")
    for forbidden in stdout_not_contains:
        if forbidden in completed.stdout:
            raise RuntimeError(f"CLI {label} unexpectedly printed: {forbidden}")
    if stderr_exact is not None and completed.stderr != stderr_exact:
        raise RuntimeError(
            f"CLI {label} stderr mismatch: " f"{completed.stderr!r} != {stderr_exact!r}"
        )
    return record


def _python_environment(source_root: Path) -> dict[str, str]:
    """Build a minimal child environment with no inherited credentials.

    PATH/PATHEXT resolve the Python and Git tools, SYSTEMROOT supports Windows
    process startup, and TEMP/TMP support Python and SQLite temporary files.
    PYTHONPATH is set explicitly to this checkout's package; UTF-8 output is
    fixed rather than inherited from the operator environment.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENVIRONMENT_KEYS
    }
    source = str(source_root.resolve() / "src")
    environment["PYTHONPATH"] = source
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _build_report(
    output: Path,
    source: Mapping[str, object],
    primary: Mapping[str, object],
    stopped: Mapping[str, object],
    cli_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Derive the human-auditable report only from closed journal facts."""
    primary_events = cast(tuple[Event, ...], primary["events"])
    stopped_events = cast(tuple[Event, ...], stopped["events"])
    primary_state = replay(primary_events)
    stopped_state = replay(stopped_events)
    answer_event = _last_event(primary_events, "question_answered")
    pause_event = _last_event(primary_events, "session_paused")
    resume_event = _last_event(primary_events, "session_resumed")
    direction_event = next(
        event
        for event in primary_events
        if event.kind == "operator_message"
        and "Prioritize a read before any write" in str(event.payload)
    )
    snapshots = {
        "question_open": _snapshot(
            primary_events, _last_event(primary_events, "question_recorded").sequence
        ),
        "paused_question_open": _snapshot(primary_events, pause_event.sequence),
        "answered_still_paused": _snapshot(primary_events, answer_event.sequence),
        "resumed": _snapshot(primary_events, resume_event.sequence),
        "final": _snapshot(primary_events, primary_events[-1].sequence),
        "stopped_answered": _snapshot(stopped_events, stopped_events[-1].sequence),
    }
    return {
        "evidence_id": "phase-0002-operator-steering-resume",
        "scope": (
            "Deterministic scripted-model operator-control evidence. It proves "
            "durability, CLI presentation, pause/answer/resume/stop mechanics, "
            "replay, and fixture acceptance. It does not claim autonomous model "
            "reasoning, live-provider behavior, or broad software capability."
        ),
        "source": dict(source),
        "package_version": "0.2.0",
        "configuration": _jsonable(CONFIGURATION),
        "configuration_digest": _configuration_digest(),
        "model": _first_payload(
            primary_events, "model_configuration_bound", "configuration"
        ),
        "runtime": _jsonable(ComponentIdentity("software-fixture-runtime", "v1")),
        "evaluator": _jsonable(ComponentIdentity("software-fixture-evaluator", "v1")),
        "task_ids": {
            "primary": "vharness-phase2-operator-steering-resume",
            "stopped": "vharness-phase2-stopped-question",
        },
        "environment_ids": {
            "primary": "software-fixture",
            "stopped": "software-fixture",
        },
        "fixed_budgets": {
            "primary": PRIMARY_BUDGETS,
            "stopped": STOPPED_BUDGETS,
        },
        "operator_interventions": {
            "classification_counts": {
                "requested": 2,
                "corrective": 2,
                "abort": 0,
            },
            "notes": (
                "Direction and pause are corrective; the question answer and "
                "resume are requested controls; the intentionally wrong answer "
                "is a rejected corrective input; no abort was issued."
            ),
        },
        "primary_run": {
            "journal": primary["journal"],
            "artifacts": primary["artifacts"],
            "status": primary_state.status.value,
            "counters": _jsonable(derive_counters(primary_events)),
            "manifest": _jsonable(derive_manifest(primary_events)),
            "journal_cursor": primary_events[-1].sequence,
            "projection_sha256": _projection_digest(primary_state),
            "accepted_lineage": [
                _jsonable(item) for item in primary_state.accepted_lineage
            ],
            "pending_effect": primary_state.pending_proposal_id,
            "question": {
                "question_id": primary["question_id"],
                "question_sequence": primary["question_sequence"],
                "answer_sequence": answer_event.sequence,
                "answer_command_id": answer_event.payload.get("command_id"),
            },
            "operator_context": {
                "message_sequence": direction_event.sequence,
                "context_assembly_sequence": primary["context_sequence"],
                "model_request_id": primary["context_request_id"],
                "visible_input_contains_direction": primary[
                    "context_contains_direction"
                ],
                "visible_input_sha256": _sha256_text(
                    str(
                        context_event_payload(
                            primary_events, cast(int, primary["context_sequence"])
                        ).get("input", "")
                    )
                ),
            },
            "pause_resume": {
                "pause_sequence": pause_event.sequence,
                "resume_sequence": resume_event.sequence,
                "before_pause_metrics": primary["before_pause_metrics"],
                "paused_metrics": primary["paused_metrics"],
                "after_suppressed_step_metrics": primary[
                    "after_suppressed_step_metrics"
                ],
                "model_calls_at_pause": primary["model_calls_at_pause"],
                "pending_effect_before_resume": primary["pending_effect_before_resume"],
                "operator_direction_after_resume": _operator_direction_at(
                    primary_events, resume_event.sequence
                ),
                "acknowledged_steering_after_resume": _snapshot(
                    primary_events, resume_event.sequence
                )["acknowledged_steering"],
                "pending_effect_after_resume": primary_state.pending_proposal_id,
            },
            "action_receipts": _action_receipt_facts(primary_events),
            "checkpoint": {
                "sequence": primary["checkpoint_sequence"],
                "cursor": cast(int, primary["checkpoint_sequence"]) - 1,
                "projection_sha256": _checkpoint_projection_digest(
                    primary_events, cast(int, primary["checkpoint_sequence"])
                ),
            },
        },
        "stopped_question_run": {
            "journal": stopped["journal"],
            "artifacts": stopped["artifacts"],
            "status": stopped_state.status.value,
            "counters": _jsonable(derive_counters(stopped_events)),
            "manifest": _jsonable(derive_manifest(stopped_events)),
            "question_id": stopped["question_id"],
            "question_sequence": stopped["question_sequence"],
            "stop_sequence": stopped["stop_sequence"],
            "answer_sequence": stopped["answer_sequence"],
            "model_calls": stopped["model_calls"],
            "pending_effect": stopped_state.pending_proposal_id,
        },
        "snapshots": snapshots,
        "context_manifest_count": sum(
            event.kind == "context_assembled" for event in primary_events
        ),
        "cli_transcript_records": list(cli_records),
        "fresh_process_verification": _read_json(output / "fresh-process-reopen.json"),
        "event_hashes": {
            "primary": _event_hashes(primary_events),
            "stopped": _event_hashes(stopped_events),
        },
    }


def _write_context_manifests(
    output: Path, primary: Mapping[str, object], stopped: Mapping[str, object]
) -> None:
    """Retain all context manifests exactly as projected from the journals."""
    records = {}
    for name, events in (
        ("primary", cast(tuple[Event, ...], primary["events"])),
        ("stopped", cast(tuple[Event, ...], stopped["events"])),
    ):
        records[name] = [
            {"sequence": event.sequence, "manifest": event.payload.get("manifest")}
            for event in events
            if event.kind == "context_assembled"
        ]
    _write_json(output / "context-manifests.json", _jsonable(records))


def _write_transcript(
    output: Path,
    primary: Mapping[str, object],
    stopped: Mapping[str, object],
    cli_records: Sequence[Mapping[str, object]],
) -> None:
    """Write a readable transcript from CLI output and durable event facts."""
    lines = [
        "PHASE-0002 operator steering/resume evidence",
        "Scope: deterministic scripted-model operator-control mechanics only.",
        "",
        "OPT-IN CLI TRANSCRIPT",
    ]
    for record in cli_records:
        lines.extend(
            [
                f"[{record['label']}] exit={record['returncode']}",
                "STDOUT:",
                str(record["stdout"]).rstrip(),
                "STDERR:",
                str(record["stderr"]).rstrip(),
                "",
            ]
        )
    lines.append("PRIMARY DURABLE EVENT TRANSCRIPT")
    lines.extend(_event_lines(cast(tuple[Event, ...], primary["events"])))
    lines.extend(["", "STOPPED-QUESTION DURABLE EVENT TRANSCRIPT"])
    lines.extend(_event_lines(cast(tuple[Event, ...], stopped["events"])))
    with (output / "transcript.txt").open(
        "w", encoding="utf-8", newline="\n"
    ) as stream:
        stream.write("\n".join(lines) + "\n")


def _event_lines(events: Sequence[Event]) -> list[str]:
    lines = []
    for event in events:
        if not hasattr(event, "kind"):
            continue
        payload = event.payload
        detail = ""
        if event.kind == "session_created":
            task = _mapping(payload.get("task"))
            detail = f"objective={task.get('objective')}"
        elif event.kind == "plan_recorded":
            detail = f"plan={payload.get('text')}"
        elif event.kind == "operator_message":
            command = _mapping(payload.get("command"))
            detail = f"command={command.get('command_id')} text={command.get('text')}"
        elif event.kind == "question_recorded":
            detail = (
                f"question_id={payload.get('question_id')} text={payload.get('text')}"
            )
        elif event.kind == "question_answered":
            detail = f"question_id={payload.get('question_id')} command={payload.get('command_id')}"
        elif event.kind == "command_rejected":
            detail = (
                f"command={payload.get('command_id')} reason={payload.get('reason')}"
            )
        elif event.kind == "action_intended":
            proposal = _mapping(payload.get("proposal"))
            detail = f"proposal={proposal.get('proposal_id')} action={proposal.get('action_name')} arguments={proposal.get('arguments')}"
        elif event.kind == "receipt_received":
            receipt = _mapping(payload.get("receipt"))
            detail = f"receipt={receipt.get('receipt_id')} proposal={receipt.get('proposal_id')} status={receipt.get('status')}"
        elif event.kind == "evaluation_received":
            receipt = _mapping(payload.get("receipt"))
            detail = f"receipt={receipt.get('receipt_id')} accepted={receipt.get('accepted')} comparison={receipt.get('comparison')}"
        elif event.kind == "lineage_promoted":
            node = _mapping(payload.get("node"))
            detail = f"evaluation_receipt={payload.get('evaluation_receipt_id')} node={node.get('node_id')}"
        elif event.kind == "context_assembled":
            manifest = _mapping(payload.get("manifest"))
            detail = (
                f"included={manifest.get('included')} omitted={manifest.get('omitted')}"
            )
        elif event.kind == "model_decided":
            detail = f"result={payload.get('result_kind')} request={payload.get('request_id')}"
        elif event.kind in {"session_paused", "session_resumed", "session_stopped"}:
            command = _mapping(payload.get("command"))
            detail = f"command={command.get('command_id')}"
        elif event.kind == "checkpoint":
            checkpoint = _mapping(payload.get("checkpoint"))
            detail = f"cursor={checkpoint.get('cursor')}"
        lines.append(f"{event.sequence:04d} {event.kind} {detail}".rstrip())
    return lines


def _build_bundle_manifest(
    output: Path,
    source: Mapping[str, object],
    primary: Mapping[str, object],
    stopped: Mapping[str, object],
) -> dict[str, object]:
    """Pin source and hashes using only relative bundle paths."""
    files = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "report.json"}:
            continue
        relative = path.relative_to(output).as_posix()
        files[relative] = {
            "sha256": _sha256_path(path),
            "size": path.stat().st_size,
        }
    report_path = output / "report.json"
    files["report.json"] = {
        "sha256": _sha256_path(report_path),
        "size": report_path.stat().st_size,
    }
    return {
        "manifest_version": 1,
        "evidence_id": "phase-0002-operator-steering-resume",
        "scope": "scripted operator-control evidence; not autonomous capability evidence",
        "source": dict(source),
        "package_version": "0.2.0",
        "configuration_digest": _configuration_digest(),
        "journals": {
            "primary": _journal_facts(
                output / cast(str, primary["journal"]),
                cast(tuple[Event, ...], primary["events"]),
            ),
            "stopped": _journal_facts(
                output / cast(str, stopped["journal"]),
                cast(tuple[Event, ...], stopped["events"]),
            ),
        },
        "artifact_stores": {
            "primary": _artifact_facts(
                output / cast(str, primary["artifacts"]),
                cast(tuple[Event, ...], primary["events"]),
            ),
            "stopped": _artifact_facts(
                output / cast(str, stopped["artifacts"]),
                cast(tuple[Event, ...], stopped["events"]),
            ),
        },
        "derived_files": {
            "report": "report.json",
            "transcript": "transcript.txt",
            "context_manifests": "context-manifests.json",
            "fresh_process_reopen": "fresh-process-reopen.json",
            "reproducer": "reproduce.py",
        },
        "files": files,
        "self_hash": "omitted to avoid recursive manifest hashing",
    }


def _journal_facts(path: Path, events: Sequence[Event]) -> dict[str, object]:
    """Return integrity, replay, and canonical event facts for one journal."""
    state = replay(events)
    integrity, foreign_keys = _sqlite_integrity(path)
    sidecars = [
        candidate.name
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    ]
    return {
        "path": path.name,
        "sha256": _sha256_path(path),
        "size": path.stat().st_size,
        "cursor": events[-1].sequence,
        "event_count": len(events),
        "event_stream_sha256": _sha256_text(canonical_json(events)),
        "projection_sha256": _projection_digest(state),
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
        "wal_sidecars": sidecars,
        "replay_status": state.status.value,
    }


def _artifact_facts(path: Path, events: Sequence[Event]) -> dict[str, object]:
    """Verify every artifact reference reachable from one journal."""
    store = _ReadOnlyArtifactStore(path)
    references = _artifact_refs(tuple(event.payload for event in events))
    checked = []
    for reference in sorted(references, key=lambda item: item.digest):
        data = store.read(reference)
        checked.append(
            {
                "digest": reference.digest,
                "size": reference.size,
                "actual_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "relative_path": (
                    f"{path.name}/{reference.digest[:2]}/{reference.digest[2:]}"
                ),
            }
        )
    return {
        "path": path.name,
        "journal_pointer": (
            (path / ".journals").read_text(encoding="utf-8").strip()
            if (path / ".journals").is_file()
            else None
        ),
        "reachable_artifacts": checked,
        "inventory": [
            {"digest": digest, "size": size} for digest, size in store.inventory()
        ],
    }


def _validate_bundle_manifest(bundle: Path, manifest: object) -> None:
    """Validate every retained file against the bundle's manifest."""
    _assert_no_reparse_components(bundle)
    if not isinstance(manifest, Mapping):
        raise RuntimeError("manifest must be a JSON object")
    files_value = manifest.get("files")
    if not isinstance(files_value, Mapping) or not files_value:
        raise RuntimeError("manifest files must be a non-empty object")
    declared: set[str] = set()
    for raw_path, raw_facts in files_value.items():
        relative = _safe_relative_path(raw_path, "manifest file")
        if relative == "manifest.json":
            raise RuntimeError("manifest must not declare itself")
        path = _safe_bundle_path(bundle, relative, "manifest file")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"manifest file is missing: {relative}")
        facts = _mapping(raw_facts)
        size = facts.get("size")
        digest = facts.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool):
            raise RuntimeError(f"manifest size is invalid: {relative}")
        if not _is_sha256(digest):
            raise RuntimeError(f"manifest digest is invalid: {relative}")
        if path.stat().st_size != size or _sha256_path(path) != digest:
            raise RuntimeError(f"manifest hash or size mismatch: {relative}")
        declared.add(relative)

    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"bundle contains a symlink: {path.name}")
        if path.is_file():
            relative = path.relative_to(bundle).as_posix()
            if relative != "manifest.json":
                actual.add(relative)
    if actual != declared:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        raise RuntimeError(
            f"manifest file set mismatch: missing={missing}, extra={extra}"
        )

    for name, raw_path in _mapping(manifest.get("derived_files")).items():
        relative = _safe_relative_path(raw_path, f"derived file {name}")
        if relative not in declared:
            raise RuntimeError(f"derived file is not retained: {relative}")


def _validate_source_pin(bundle: Path, source_root: Path, manifest: object) -> None:
    """Prove the pinned Git revision contains the bundled generator source."""
    source = _mapping(_mapping(manifest).get("source"))
    revision = source.get("git_revision")
    generator_digest = source.get("generator_sha256")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("manifest source Git revision is invalid")
    if source.get("git_dirty") is not False:
        raise RuntimeError("manifest source is not a clean pinned revision")
    if not _is_sha256(generator_digest):
        raise RuntimeError("manifest generator digest is invalid")
    reproduce = bundle / "reproduce.py"
    if not reproduce.is_file():
        raise RuntimeError("bundled reproducer is missing")
    source_root = source_root.resolve()
    repo = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if repo.returncode != 0:
        raise RuntimeError("source root is not a Git repository")
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"pinned source revision is unavailable: {revision}")
    generator = subprocess.run(
        ["git", "show", f"{revision}:tools/phase2_operator_evidence.py"],
        cwd=source_root,
        capture_output=True,
        check=False,
    )
    if generator.returncode != 0:
        raise RuntimeError("pinned source revision lacks the evidence generator")
    generator_bytes = cast(bytes, generator.stdout)
    bundled_bytes = reproduce.read_bytes()
    if _normalized_sha256(generator_bytes) != _normalized_sha256(bundled_bytes):
        raise RuntimeError("bundled reproducer differs from the pinned generator")
    if generator_digest != _normalized_sha256(bundled_bytes):
        raise RuntimeError("manifest generator digest does not match reproducer")


def _validate_journal_facts(name: str, facts: Mapping[str, object]) -> None:
    """Require the retained journal to have durable, sidecar-free integrity."""
    if facts.get("integrity_check") != "ok":
        raise RuntimeError(f"SQLite integrity failed for {name}")
    if facts.get("foreign_key_check") != []:
        raise RuntimeError(f"SQLite foreign-key check failed for {name}")
    if facts.get("wal_sidecars") != []:
        raise RuntimeError(f"SQLite sidecar present for {name}")


def _validate_artifact_facts(
    bundle: Path,
    journal_path: Path,
    artifacts_path: Path,
    facts: Mapping[str, object],
) -> None:
    """Require relative pointers, valid layout, and verified artifact objects."""
    _assert_no_reparse_components(bundle)
    pointer = facts.get("journal_pointer")
    relative = _safe_relative_path(
        pointer, "artifact journal pointer", allow_parent=True
    )
    pointer_path = _safe_bundle_path(
        bundle, f"{artifacts_path.name}/{relative}", "artifact journal pointer"
    ).resolve()
    if pointer_path != journal_path.resolve() or not pointer_path.is_file():
        raise RuntimeError("artifact journal pointer is not relative to its journal")
    try:
        pointer_path.relative_to(bundle.resolve())
    except ValueError as exc:
        raise RuntimeError("artifact journal pointer escapes the bundle") from exc

    for path in artifacts_path.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative_path = path.relative_to(artifacts_path).as_posix()
        if relative_path == ".journals":
            continue
        if relative_path == ".software-fixture-state":
            # The adapter-owned checkpoint is bundle-hashed and decoded by
            # SoftwareFixture during fresh-process reopen. Keep this exact
            # metadata path separate from content-addressed artifact objects.
            continue
        parts = PurePosixPath(relative_path).parts
        if (
            len(parts) != 2
            or len(parts[0]) != 2
            or len(parts[1]) != 62
            or not all(char in "0123456789abcdef" for char in parts[0] + parts[1])
        ):
            raise RuntimeError(f"unexpected artifact-store file: {relative_path}")

    reachable = facts.get("reachable_artifacts")
    if not isinstance(reachable, list):
        raise RuntimeError("artifact reachable inventory is invalid")
    for item in reachable:
        record = _mapping(item)
        digest = record.get("digest")
        size = record.get("size")
        actual_size = record.get("actual_size")
        sha256 = record.get("sha256")
        relative_path = record.get("relative_path")
        if (
            not _is_sha256(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or actual_size != size
            or sha256 != digest
        ):
            raise RuntimeError("reachable artifact digest or size mismatch")
        assert isinstance(digest, str)
        expected_path = f"{artifacts_path.name}/{digest[:2]}/{digest[2:]}"
        if relative_path != expected_path:
            raise RuntimeError("reachable artifact path is not relative and canonical")


def _validate_report_against_events(
    report: object, streams: Mapping[str, tuple[Event, ...]]
) -> None:
    """Recompute operator report claims from both canonical journal streams."""
    mapping = _mapping(report)
    if mapping.get("evidence_id") != BUNDLE_ID:
        raise RuntimeError("report evidence ID is invalid")
    if mapping.get("configuration_digest") != _configuration_digest():
        raise RuntimeError("report configuration digest is invalid")
    expected_hashes: dict[str, object] = {}
    for name, events in streams.items():
        state = replay(events)
        counters = derive_counters(events)
        section_name = "primary_run" if name == "primary" else "stopped_question_run"
        section = _mapping(mapping.get(section_name))
        _require_equal(
            f"{name} report status", section.get("status"), state.status.value
        )
        _require_equal(
            f"{name} report counters", section.get("counters"), _jsonable(counters)
        )
        _require_equal(
            f"{name} report manifest",
            section.get("manifest"),
            _jsonable(derive_manifest(events)),
        )
        _require_equal(
            f"{name} report pending effect",
            section.get("pending_effect"),
            state.pending_proposal_id,
        )
        if name == "primary":
            _require_equal(
                f"{name} report cursor",
                section.get("journal_cursor"),
                events[-1].sequence,
            )
            _require_equal(
                f"{name} report projection",
                section.get("projection_sha256"),
                _projection_digest(state),
            )
            _require_equal(
                f"{name} report lineage",
                section.get("accepted_lineage"),
                [_jsonable(item) for item in state.accepted_lineage],
            )
        expected_hashes[name] = _event_hashes(events)
    _require_equal(
        "operator report event hashes", mapping.get("event_hashes"), expected_hashes
    )
    primary = _mapping(mapping.get("primary_run"))
    primary_events = streams["primary"]
    _require_equal(
        "operator report action receipts",
        primary.get("action_receipts"),
        _action_receipt_facts(primary_events),
    )


def _require_equal(label: str, actual: object, expected: object) -> None:
    """Raise a concise error when a retained fact differs from its manifest."""
    if actual != expected:
        raise RuntimeError(f"{label} mismatch")


def _safe_relative_path(
    value: object, label: str, *, allow_parent: bool = False
) -> str:
    """Parse a canonical relative POSIX path and reject traversal or roots."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a relative path")
    if "\\" in value:
        raise RuntimeError(f"{label} must use POSIX separators")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(
            part in ({"", "."} if allow_parent else {"", ".", ".."})
            for part in posix.parts
        )
        or posix.as_posix() != value
    ):
        raise RuntimeError(f"{label} must be canonical and relative")
    return value


def _safe_bundle_path(bundle: Path, relative: str, label: str) -> Path:
    """Resolve a retained path while rejecting links, junctions, and escapes."""
    if _is_reparse_point(bundle):
        raise RuntimeError(f"{label} traverses a reparse point")
    root = bundle.resolve()
    candidate = bundle.joinpath(*PurePosixPath(relative).parts)
    current = bundle
    for part in PurePosixPath(relative).parts:
        current /= part
        if _is_reparse_point(current):
            raise RuntimeError(f"{label} traverses a reparse point")
        try:
            current.resolve().relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"{label} escapes the bundle") from exc
    return candidate


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect bundle path: {path}") from exc
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_no_reparse_components(root: Path) -> None:
    """Walk retained bundle components without following reparse points."""
    if _is_reparse_point(root):
        raise RuntimeError("bundle root is a reparse point")
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"cannot inspect bundle directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise RuntimeError(f"bundle contains a reparse point: {path.name}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)


def _is_sha256(value: object) -> bool:
    """Return whether a value is a lowercase SHA-256 digest."""
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _sqlite_integrity(path: Path) -> tuple[str, list[list[object]]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys: list[list[object]] = []
        for row in connection.execute("PRAGMA foreign_key_check"):
            foreign_keys.append([value for value in row])
        return integrity, foreign_keys
    finally:
        connection.close()


def _snapshot(events: Sequence[Event], cursor: int) -> dict[str, object]:
    prefix = tuple(event for event in events if event.sequence <= cursor)
    state = replay(prefix)
    view = build_operator_view(prefix[0].session_id, state, prefix)
    return {
        "cursor": cursor,
        "projection_sha256": _projection_digest(state),
        "status": state.status.value,
        "objective": state.task.objective,
        "objective_version": state.task.objective_version,
        "current_plan": view.current_plan,
        "operator_direction": view.operator_direction,
        "questions": view.questions,
        "acknowledged_steering": view.acknowledged_steering,
        "budgets": view.remaining_budgets,
        "pending_effect": state.pending_proposal_id,
        "lineage_head": state.lineage_head.node_id,
        "action_receipt_stream": view.action_receipt_stream,
    }


def _event_metrics(events: Sequence[Event]) -> dict[str, int | None]:
    derived = derive_counters(events)
    return {
        "model_calls": derived.model_calls,
        "action_proposals": sum(event.kind == "action_intended" for event in events),
        "runtime_receipts": derived.runtime_receipts,
        "evaluations": derived.evaluations,
        "accepted_commits": derived.commits,
    }


def _action_receipt_facts(events: Sequence[Event]) -> list[dict[str, object]]:
    receipts = {
        _mapping(event.payload.get("receipt")).get("proposal_id"): event
        for event in events
        if event.kind == "receipt_received"
        and isinstance(event.payload.get("receipt"), Mapping)
    }
    facts = []
    for event in events:
        if event.kind != "action_intended":
            continue
        proposal = _mapping(event.payload.get("proposal"))
        proposal_id = proposal.get("proposal_id")
        receipt = receipts.get(proposal_id)
        receipt_payload = _mapping(receipt.payload.get("receipt")) if receipt else {}
        facts.append(
            {
                "proposal_sequence": event.sequence,
                "proposal_id": proposal_id,
                "action_name": proposal.get("action_name"),
                "receipt_sequence": receipt.sequence if receipt else None,
                "receipt_id": receipt_payload.get("receipt_id"),
                "status": receipt_payload.get("status"),
            }
        )
    return facts


def _context_after_operator_direction(events: Sequence[Event]) -> Event:
    message = next(
        event
        for event in events
        if event.kind == "operator_message"
        and "Prioritize a read before any write" in str(event.payload)
    )
    return next(
        event
        for event in events
        if event.kind == "model_intended" and event.sequence > message.sequence
    )


def context_event_payload(
    events: Sequence[Event], sequence: int
) -> Mapping[str, object]:
    """Return one durable model-intent payload by event sequence."""
    event = next(event for event in events if event.sequence == sequence)
    return _mapping(event.payload)


def _snapshot_operator_direction(events: Sequence[Event], cursor: int) -> str | None:
    return build_operator_view(
        events[0].session_id,
        replay(tuple(event for event in events if event.sequence <= cursor)),
        tuple(event for event in events if event.sequence <= cursor),
    ).operator_direction


def _operator_direction_at(events: Sequence[Event], cursor: int) -> str | None:
    return _snapshot_operator_direction(events, cursor)


def _view_matches_state(view: SessionView, state: SessionState) -> bool:
    return (
        view.status is state.status
        and view.task.objective == state.task.objective
        and view.task.objective_version == state.task.objective_version
        and view.lineage_head.node_id == state.lineage_head.node_id
        and view.pending_proposal_id == state.pending_proposal_id
        and view.wait_reason == state.wait_reason
    )


def _artifact_refs(value: object) -> set[ArtifactRef]:
    found: set[ArtifactRef] = set()
    if isinstance(value, Mapping):
        digest = value.get("digest")
        size = value.get("size")
        media_type = value.get("media_type")
        provenance = value.get("provenance")
        if (
            isinstance(digest, str)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and isinstance(media_type, str)
            and isinstance(provenance, str)
        ):
            try:
                found.add(ArtifactRef(digest, size, media_type, provenance))
            except (TypeError, ValueError):
                pass
        for item in value.values():
            found.update(_artifact_refs(item))
    elif isinstance(value, (tuple, list)):
        for item in value:
            found.update(_artifact_refs(item))
    return found


def _all_events(journal: SqliteJournal, session_id: str) -> tuple[Event, ...]:
    events: list[Event] = []
    cursor = 0
    while True:
        page = journal.events(session_id, after_sequence=cursor, limit=100)
        if not page:
            return tuple(events)
        events.extend(page)
        cursor = page[-1].sequence


def _last_event(events: Sequence[Event], kind: str) -> Event:
    return next(event for event in reversed(events) if event.kind == kind)


def _last_event_before(events: Sequence[Event], kind: str, cursor: int) -> Event:
    return next(
        event
        for event in reversed(events)
        if event.kind == kind and event.sequence < cursor
    )


def _first_payload(events: Sequence[Event], kind: str, field: str) -> object:
    return next(event.payload.get(field) for event in events if event.kind == kind)


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _string_field(payload: object, name: str) -> str:
    value = _mapping(payload).get(name)
    if not isinstance(value, str):
        raise RuntimeError(f"durable field {name} is not a string")
    return value


def _action_ids(events: Sequence[Event]) -> list[str]:
    return [
        _string_field(event.payload.get("proposal"), "proposal_id")
        for event in events
        if event.kind == "action_intended"
    ]


def _action_names(events: Sequence[Event]) -> list[str]:
    """Return the durable action names in execution order."""
    return [
        _string_field(event.payload.get("proposal"), "action_name")
        for event in events
        if event.kind == "action_intended"
    ]


def _assert_action_receipt_sequence(events: Sequence[Event]) -> None:
    """Require the exact exercised action sequence and one receipt per action."""
    action_events = [event for event in events if event.kind == "action_intended"]
    receipt_events = [event for event in events if event.kind == "receipt_received"]
    action_names = _action_names(events)
    if tuple(action_names) != PRIMARY_ACTIONS:
        raise RuntimeError(
            f"unexpected action sequence: {action_names!r} != {PRIMARY_ACTIONS!r}"
        )
    proposal_ids = [
        _string_field(event.payload.get("proposal"), "proposal_id")
        for event in action_events
    ]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise RuntimeError("external proposal identity was duplicated")
    if len(receipt_events) != len(action_events):
        raise RuntimeError("an action did not receive exactly one runtime receipt")
    receipt_proposal_ids = [
        _string_field(event.payload.get("receipt"), "proposal_id")
        for event in receipt_events
    ]
    if len(set(receipt_proposal_ids)) != len(receipt_proposal_ids):
        raise RuntimeError("a proposal received more than one runtime receipt")
    if set(receipt_proposal_ids) != set(proposal_ids):
        raise RuntimeError("runtime receipts were not correlated to the actions")


def _event_hashes(events: Sequence[Event]) -> dict[str, str]:
    return {
        str(event.sequence): _sha256_text(canonical_json(event)) for event in events
    }


def _checkpoint_projection_digest(events: Sequence[Event], sequence: int) -> str:
    checkpoint = next(event for event in events if event.sequence == sequence)
    return _string_field(checkpoint.payload.get("checkpoint"), "projection_digest")


def _journal_name(name: str) -> str:
    return "session.sqlite3" if name == "primary" else "stopped-question.sqlite3"


def _artifact_name(name: str) -> str:
    return "artifacts" if name == "primary" else "stopped-artifacts"


def _run_identity() -> RunIdentity:
    return RunIdentity(
        package_version="0.2.0",
        configuration_digest=_configuration_digest(),
        runtime=ComponentIdentity("software-fixture-runtime", "v1"),
        evaluator=ComponentIdentity("software-fixture-evaluator", "v1"),
    )


def _configuration_digest() -> str:
    return _sha256_text(canonical_json(CONFIGURATION))


def _source_identity(source_root: Path) -> dict[str, object]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    generator = source_root.resolve() / "tools" / "phase2_operator_evidence.py"
    if not generator.is_file():
        raise RuntimeError(f"evidence generator is missing: {generator}")
    return {
        "git_revision": revision,
        "git_dirty": dirty,
        "generator_sha256": _normalized_sha256(generator.read_bytes()),
    }


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_sha256(value: bytes) -> str:
    """Hash source text while treating checkout newline conversion as cosmetic."""
    return hashlib.sha256(value.replace(b"\r\n", b"\n")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _projection_digest(state: SessionState) -> str:
    # Keep the report digest compatible with retained pre-correction bundles.
    # Event hashes and replay still cover the newer model-operation projection.
    return _legacy_projection_digest(state)


def _write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonable(value: object) -> object:
    return json.loads(canonical_json(value))


def _normalize_text_file(path: Path) -> None:
    """Canonicalize generated pointer text without changing binary evidence."""
    path.write_bytes(_normalized_bytes(path.read_bytes()))


def _normalized_bytes(value: bytes) -> bytes:
    """Normalize only CRLF pairs; evidence hashes the resulting raw bytes."""
    return value.replace(b"\r\n", b"\n")


if __name__ == "__main__":
    raise SystemExit(main())
