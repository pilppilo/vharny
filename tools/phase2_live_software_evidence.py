"""Run and verify live PHASE-0002 software-fixture evidence.

The live run uses the OpenAI-compatible LongCat endpoint through
``CompatibleModel``.  The model chooses typed results from the visible
request; this harness does not provide an action script or hidden validator
data.  The retained bundle contains no credential material.
"""

from __future__ import annotations

# This is a bounded evidence harness rather than a product module.  Keep the
# retained facts explicit and auditable.
# pylint: disable=duplicate-code,too-many-arguments,too-many-branches
# pylint: disable=too-many-locals,too-many-return-statements,too-many-statements
# pylint: disable=super-init-not-called,too-many-lines

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Mapping, Sequence, cast

from vharness.agent import (
    BudgetLimits,
    CompatibleModel,
    CompletionMode,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import (
    MAX_JSON_BYTES,
    canonical_json,
    parse_json,
    parse_json_object,
)
from vharness.agent.evidence import counters as derive_counters
from vharness.agent.evidence import manifest as derive_manifest
from vharness.agent.environments import SoftwareEvaluator, SoftwareFixture
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ArtifactRef,
    ComponentIdentity,
    Event,
    RunIdentity,
    SessionView,
)
from vharness.agent.replay import (
    _legacy_projection_digest,
    replay,
)
from vharness.agent.session_state import state_ref_from
from vharness.agent.transitions import SessionState
from vharness.generators.openai_compat import OpenAICompatible

SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "phase-0002-live-software"
SESSION_ID = "vharness-phase2-live-software-longcat"
TASK_ID = "vharness-phase2-live-software"
OBJECTIVE = "Set value to 1 in main.py and verify it with the fixture test."
PROVIDER_NAME = "LongCat"
BASE_URL = "https://api.longcat.ai/openai/v1/"
MODEL_ID = "LongCat-2.0"
CREDENTIAL_ENV = "LONGCAT_API_KEY"
CONTEXT_WINDOW_TOKENS = 4096
TOKEN_BUDGET = CONTEXT_WINDOW_TOKENS
MAX_OUTPUT_TOKENS = 1024
REPAIR_BUDGET = 1
MAX_MODEL_CALLS = 8
MAX_ACTIONS = 6
MAX_EVALUATIONS = 2
MAX_RETAINED_FILE_BYTES = MAX_JSON_BYTES
_LEGACY_TERMINAL_CURSOR_REVISIONS = frozenset(
    {"0138047de4fe60cd15a2c9ab461768a68d322160"}
)
_CHILD_ENVIRONMENT_KEYS = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP"})

CONFIGURATION = {
    "evidence": BUNDLE_ID,
    "mode": "unscripted-compatible-model",
    "provider": {
        "name": PROVIDER_NAME,
        "base_url": BASE_URL,
        "credential_env": CREDENTIAL_ENV,
    },
    "model": {
        "model_id": MODEL_ID,
        "reserved_output_tokens": MAX_OUTPUT_TOKENS,
        "per_decision_context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "temperature": 0.0,
        "timeout_seconds": 120.0,
        "max_retries": 0,
        "length_retries": 0,
        "repair_budget": REPAIR_BUDGET,
        "token_budget": CONTEXT_WINDOW_TOKENS,
    },
    "task": {
        "task_id": TASK_ID,
        "objective": OBJECTIVE,
        "completion_mode": "finite",
        "success_evidence": "external software-fixture evaluator acceptance",
    },
    "budgets": {
        "model_calls": MAX_MODEL_CALLS,
        "per_decision_context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "output_tokens": MAX_OUTPUT_TOKENS,
        "repair_budget": REPAIR_BUDGET,
        "actions": MAX_ACTIONS,
        "evaluations": MAX_EVALUATIONS,
        "coordinator_retries": 0,
    },
    "environment": {
        "environment_id": "software-fixture",
        "runtime": "software-fixture-runtime:v1",
        "evaluator": "software-fixture-evaluator:v1",
    },
}


class _ReadOnlyJournal(SqliteJournal):
    """SQLite journal adapter that refuses all writes during verification."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._connection = sqlite3.connect(
            self._path.as_uri() + "?mode=ro&immutable=1", uri=True
        )

    @property
    def path(self) -> Path:
        """Return the immutable journal path."""
        return self._path

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[Event, ...]:
        """Read one ordered journal page without opening SQLite writable."""
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
        """Reject any attempted durable mutation during verification."""
        del expected_cursor
        raise RuntimeError("read-only evidence verification cannot append events")

    def close(self) -> None:
        """Close without checkpointing or otherwise mutating the database."""
        self._connection.close()


class _ReadOnlyArtifactStore(ArtifactStore):
    """ArtifactStore view whose constructor and journal registration are read-only."""

    def __init__(self, path: Path) -> None:
        self._root = path.resolve()
        self._lock = threading.RLock()

    def register_journal(self, _path: str | Path) -> None:
        """Do not mutate the retained ``.journals`` pointer."""


def main(argv: list[str] | None = None) -> int:
    """Generate, verify, or tamper-test a live evidence bundle."""
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
    tamper = subparsers.add_parser("tamper-tests")
    tamper.add_argument("--bundle", type=Path, required=True)
    tamper.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    arguments = parser.parse_args(argv)
    if arguments.command == "generate":
        generate_bundle(arguments.output, arguments.source_root)
    elif arguments.command == "verify":
        verify_bundle(arguments.bundle, arguments.source_root)
    elif arguments.command == "record-fresh":
        _write_json(
            arguments.bundle.resolve() / "fresh-process-reopen.json",
            {"run": _fresh_process_result(arguments.bundle.resolve())},
        )
    else:
        tamper_tests(arguments.bundle, arguments.source_root)
    return 0


def generate_bundle(output: Path, source_root: Path) -> None:
    """Run LongCat once from a clean source revision and retain evidence."""
    source = _source_identity(source_root)
    if source["git_dirty"]:
        raise RuntimeError("live evidence generation requires a clean source worktree")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"evidence output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    key = _credential()
    journal = SqliteJournal(output / "session.sqlite3")
    artifacts = ArtifactStore(output / "artifacts")
    fixture = SoftwareFixture(artifacts)
    task = TaskSpec(
        task_id=TASK_ID,
        objective=OBJECTIVE,
        completion_mode=CompletionMode.FINITE,
        success_evidence="external software-fixture evaluator acceptance",
        budgets=BudgetLimits(
            actions=MAX_ACTIONS,
            model_calls=MAX_MODEL_CALLS,
            evaluations=MAX_EVALUATIONS,
        ),
        environment_id=fixture.environment_id,
        evaluation_contract_id="software-fixture-evaluator-v1",
    )
    identity = _run_identity()
    session = Session.create(
        SESSION_ID,
        task,
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
        run_identity=identity,
        initial_observation=fixture.initial_observation(SESSION_ID),
    )
    generator = OpenAICompatible(
        BASE_URL,
        MODEL_ID,
        key,
        timeout=120.0,
        max_retries=0,
        length_retries=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.0,
        cache_path=None,
    )
    generator.provider_id = PROVIDER_NAME
    model = CompatibleModel(
        generator,
        model_id=MODEL_ID,
        repair_budget=REPAIR_BUDGET,
    )
    try:
        _run_session(session, model)
        generator.close()
        journal.close()
    except Exception as exc:
        generator.close()
        journal.close()
        message = _redact(str(exc), key)
        raise RuntimeError(
            "LongCat live acceptance failed; no alternate provider/model was tried: "
            + message
        ) from exc

    events = _all_events(_ReadOnlyJournal(output / "session.sqlite3"), SESSION_ID)
    if not events:
        raise RuntimeError("live run produced no durable events")
    _write_json(output / "context-manifests.json", _context_manifests(events))
    _write_json(output / "transcript.json", _transcript(events))
    reproduce = output / "reproduce.py"
    reproduce.write_bytes(_normalized_bytes(Path(__file__).read_bytes()))
    _run_fresh_process(output, source_root, "record-fresh")
    report = _build_report(output, source, events, model)
    _assert_secret_absent(output, key)
    _write_json(output / "report.json", report)
    manifest = _build_bundle_manifest(output, source, events)
    _write_json(output / "manifest.json", manifest)
    _run_fresh_process(output, source_root, "verify")
    _assert_secret_absent(output, key)
    print(canonical_json(manifest))


def _run_session(session: Session, model: CompatibleModel) -> None:
    """Give the compatible model bounded opportunities until completion."""
    for _ in range(MAX_MODEL_CALLS):
        if session.view().status is SessionStatus.COMPLETED:
            return
        session.step(model, token_budget=TOKEN_BUDGET)
    if session.view().status is not SessionStatus.COMPLETED:
        raise RuntimeError(
            f"fixture did not complete within {MAX_MODEL_CALLS} model-call steps"
        )


def verify_bundle(bundle: Path, source_root: Path) -> None:
    """Strictly verify a retained bundle without changing any bundle file."""
    _assert_no_reparse_components(bundle)
    bundle = bundle.resolve()
    manifest = _read_json(_safe_bundle_path(bundle, "manifest.json", "bundle manifest"))
    _validate_bundle_manifest(bundle, manifest)
    _validate_source_pin(bundle, source_root, manifest)
    report = _read_json(_safe_bundle_path(bundle, "report.json", "bundle report"))
    _validate_report(report, manifest)
    journal_path = _safe_bundle_path(bundle, "session.sqlite3", "session journal")
    artifacts_path = _safe_bundle_path(bundle, "artifacts", "artifact store")
    journal = _ReadOnlyJournal(journal_path)
    try:
        events = _all_events(journal, SESSION_ID)
        if not events:
            raise RuntimeError("live journal has no events")
        facts = _journal_facts(journal_path, events)
        _require_equal(
            "journal facts", facts, _mapping(_mapping(manifest).get("journal"))
        )
        _validate_journal_facts(facts)
        artifact_facts = _artifact_facts(artifacts_path, events)
        _require_equal(
            "artifact facts",
            artifact_facts,
            _mapping(_mapping(manifest).get("artifacts")),
        )
        _validate_artifact_facts(bundle, journal_path, artifacts_path, artifact_facts)
        _validate_live_events(events, report)
        _validate_report_against_events(report, events)
        fresh = _read_json(
            _safe_bundle_path(
                bundle, "fresh-process-reopen.json", "fresh-process record"
            )
        )
        actual_fresh = {"run": _fresh_process_result(bundle)}
        _require_equal("stored fresh-process verification", actual_fresh, fresh)
        _validate_report_fresh_process(
            _mapping(report).get("fresh_process_verification"),
            fresh,
            _mapping(_mapping(report).get("source")).get("git_revision"),
            derive_counters(events),
        )
    finally:
        journal.close()
    print(canonical_json(report))


def tamper_tests(bundle: Path, source_root: Path) -> None:
    """Prove report, journal, and artifact tampering are rejected."""
    bundle = bundle.resolve()
    before = _tree_bytes(bundle)
    verify_bundle(bundle, source_root)
    if before != _tree_bytes(bundle):
        raise RuntimeError("verification mutated the original bundle")
    with tempfile.TemporaryDirectory(prefix="vharness-tamper-") as temporary:
        root = Path(temporary) / "bundle"
        shutil.copytree(bundle, root)
        cases = [
            ("report.json", root / "report.json"),
            ("session.sqlite3", root / "session.sqlite3"),
        ]
        artifact = next(
            path
            for path in (root / "artifacts").rglob("*")
            if path.is_file() and path.name != ".journals"
        )
        cases.append(("artifacts", artifact))
        for label, target in cases:
            original = target.read_bytes()
            target.write_bytes(original + b"\ntampered\n")
            try:
                verify_bundle(root, source_root)
            except (OSError, RuntimeError, sqlite3.Error):
                pass
            else:
                raise RuntimeError(f"tamper was accepted: {label}")
            target.write_bytes(original)
    if before != _tree_bytes(bundle):
        raise RuntimeError("tamper tests changed the original bundle")
    print("tamper-tests: report, journal, and artifact mutations rejected")


def _fresh_process_result(bundle: Path) -> dict[str, object]:
    """Reopen and replay the session through read-only dependencies."""
    journal = _ReadOnlyJournal(
        _safe_bundle_path(bundle, "session.sqlite3", "session journal")
    )
    try:
        events = _all_events(journal, SESSION_ID)
        if not events or events[0].kind != "session_created":
            raise RuntimeError("live journal lacks its creation event")
        state = replay(events)
        created_environment = _mapping(events[0].payload.get("environment"))
        initial_state_payload = created_environment.get("initial_state")
        if not isinstance(initial_state_payload, Mapping):
            raise RuntimeError("live journal creation lacks environment initial state")
        artifacts = _ReadOnlyArtifactStore(
            _safe_bundle_path(bundle, "artifacts", "artifact store")
        )
        environment = SoftwareFixture(
            artifacts, initial_state=state_ref_from(initial_state_payload)
        )
        opened = Session.open(
            SESSION_ID,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=environment,
            evaluator=SoftwareEvaluator(environment, artifacts),
            run_identity=_run_identity(),
        )
        opened_view = opened.view()
        _validate_current_projection(
            opened_view, state, events[-1].sequence, "live journal"
        )
        counters = derive_counters(events)
        manifest = derive_manifest(events)
        opened_counters = opened.counters()
        opened_manifest = opened.manifest()
        result = {
            "session_id": SESSION_ID,
            "cursor": events[-1].sequence,
            "status": state.status.value,
            "projection_sha256": _projection_digest(state),
            "opened_view": _jsonable(opened_view),
            "accepted_lineage": [_jsonable(item) for item in state.accepted_lineage],
            "counters": _jsonable(counters),
            "manifest": _jsonable(manifest),
            "opened_view_matches_replay": True,
            "counters_match": opened_counters == counters,
            "manifest_match": opened_manifest == manifest,
        }
        if not all(
            (
                result["opened_view_matches_replay"],
                result["counters_match"],
                result["manifest_match"],
            )
        ):
            raise RuntimeError("fresh process projections disagree")
        return result
    finally:
        journal.close()


def _run_fresh_process(output: Path, source_root: Path, command: str) -> None:
    """Run the bundled reproducer in a process with no credential variable."""
    environment = _python_environment(source_root)
    environment.pop(CREDENTIAL_ENV, None)
    completed = subprocess.run(
        [
            sys.executable,
            "reproduce.py",
            command,
            "--bundle",
            ".",
            "--source-root",
            str(source_root.resolve()),
        ],
        cwd=output,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"fresh process {command} failed: {completed.stdout}\n{completed.stderr}"
        )


def _build_report(
    output: Path,
    source: Mapping[str, object],
    events: Sequence[Event],
    model: CompatibleModel,
) -> dict[str, object]:
    """Derive the human-facing report from closed durable facts."""
    state = replay(events)
    counters = derive_counters(events)
    attempts = _model_attempt_facts(events)
    return {
        "evidence_id": BUNDLE_ID,
        "scope": (
            "Unscripted live CompatibleModel acceptance on the software fixture. "
            "The model selected typed actions/evaluation from the visible request; "
            "the harness supplied no scripted action sequence or hidden validator data."
        ),
        "source": dict(source),
        "package_version": "0.2.0",
        "configuration": _jsonable(CONFIGURATION),
        "configuration_digest": _configuration_digest(),
        "model_configuration_bound": _first_payload(
            events, "model_configuration_bound", "configuration"
        ),
        "run": {
            "session_id": SESSION_ID,
            "task_id": TASK_ID,
            "status": state.status.value,
            "accepted": state.status is SessionStatus.COMPLETED,
            "journal_cursor": events[-1].sequence,
            "projection_sha256": _projection_digest(state),
            "counters": _jsonable(counters),
            "session_counters": _jsonable(counters),
            "manifest": _jsonable(derive_manifest(events)),
            "accepted_lineage": [_jsonable(item) for item in state.accepted_lineage],
            "model_calls_observed_by_adapter": model.calls,
            "action_names": _action_names(events),
            "action_receipts": _action_receipts(events),
            "evaluation_receipts": _evaluation_receipts(events),
            "attempts": attempts,
            "unknown_usage_operation_ids": list(
                state.ledger.unknown_usage_operation_ids
            ),
            "unknown_cost_operation_ids": list(state.ledger.unknown_cost_operation_ids),
        },
        "acceptance_claims": {
            "scripted_action_sequence_supplied": False,
            "hidden_validator_data_supplied": False,
            "provider_rejected": False,
            "external_evaluator_accepted": _accepted_evaluation(events),
            "lineage_promoted": any(
                event.kind == "lineage_promoted" for event in events
            ),
        },
        "fresh_process_verification": _read_json(output / "fresh-process-reopen.json"),
        "event_hashes": {
            str(event.sequence): _sha256_text(canonical_json(event)) for event in events
        },
    }


def _validate_report(report: object, manifest: object) -> None:
    """Validate report identity and fixed live-run claims."""
    mapping = _mapping(report)
    if mapping.get("evidence_id") != BUNDLE_ID:
        raise RuntimeError("report evidence ID is invalid")
    if mapping.get("configuration_digest") != _configuration_digest():
        raise RuntimeError("report configuration digest is invalid")
    configured_model = _mapping(_mapping(mapping.get("configuration")).get("model"))
    if configured_model.get("reserved_output_tokens") != MAX_OUTPUT_TOKENS:
        raise RuntimeError("report output reservation is invalid")
    if (
        configured_model.get("per_decision_context_window_tokens")
        != CONTEXT_WINDOW_TOKENS
    ):
        raise RuntimeError("report context-window budget is invalid")
    bound = _mapping(mapping.get("model_configuration_bound"))
    bound_output = _mapping(bound.get("output_settings"))
    if bound_output.get("reserved_output_tokens") != MAX_OUTPUT_TOKENS:
        raise RuntimeError("bound model output reservation is invalid")
    if bound.get("token_budget") != CONTEXT_WINDOW_TOKENS:
        raise RuntimeError("bound model context-window budget is invalid")
    source = _mapping(mapping.get("source"))
    manifest_source = _mapping(_mapping(manifest).get("source"))
    if source != manifest_source:
        raise RuntimeError("report and manifest source pins differ")
    claims = _mapping(mapping.get("acceptance_claims"))
    if claims.get("scripted_action_sequence_supplied") is not False:
        raise RuntimeError("live report permits scripted action control")
    if claims.get("hidden_validator_data_supplied") is not False:
        raise RuntimeError("live report permits hidden validator data")
    run = _mapping(mapping.get("run"))
    if run.get("status") != "completed" or run.get("accepted") is not True:
        raise RuntimeError("live report does not record completion")
    counters = _mapping(run.get("counters"))
    if counters.get("model_calls", 0) > MAX_MODEL_CALLS:
        raise RuntimeError("model-call limit exceeded")
    if counters.get("input_tokens") is None or counters.get("output_tokens") is None:
        raise RuntimeError("model token reconciliation is unknown")
    if counters.get("duration_ms") is None:
        raise RuntimeError("model duration reconciliation is unknown")
    if not claims.get("external_evaluator_accepted") or not claims.get(
        "lineage_promoted"
    ):
        raise RuntimeError("external acceptance or lineage promotion is absent")


def _validate_live_events(events: Sequence[Event], report: object) -> None:
    """Validate model, action, evaluation, and accounting facts independently."""
    if events[0].kind != "session_created" or events[-1].sequence != len(events):
        raise RuntimeError("live journal sequence is not contiguous")
    if any(event.session_id != SESSION_ID for event in events):
        raise RuntimeError("live journal contains another session")
    model_intended = [event for event in events if event.kind == "model_intended"]
    terminal_models = [
        event for event in events if event.kind in {"model_decided", "model_malformed"}
    ]
    if not model_intended or len(model_intended) != len(terminal_models):
        raise RuntimeError("model intent and terminal event counts differ")
    intents_by_request: dict[str, Event] = {}
    bound_configuration = _mapping(
        _first_payload(events, "model_configuration_bound", "configuration")
    )
    bound_model_id = bound_configuration.get("model_id")
    for event in model_intended:
        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("live model intent request ID is invalid")
        if request_id in intents_by_request:
            raise RuntimeError("live model intents contain duplicate request IDs")
        _validate_model_cursor(event, is_intent=True)
        if event.payload.get("model_id") != bound_model_id:
            raise RuntimeError("live model intent model ID differs from its binding")
        intents_by_request[request_id] = event
    state = replay(events)
    if len(state.model_operations) != len(model_intended) or any(
        not operation.settled for operation in state.model_operations
    ):
        raise RuntimeError("live model operations are not one-to-one and settled")
    for event in terminal_models:
        payload = event.payload
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("live model terminal request ID is invalid")
        intended = intents_by_request.get(request_id)
        if intended is None:
            raise RuntimeError("live model terminal has no matching intent")
        _validate_model_cursor(event, is_intent=False)
        if payload.get("model_id") != bound_model_id:
            raise RuntimeError("live model terminal model ID differs from its binding")
        intended_cursor = intended.payload.get("cursor")
        if payload.get("cursor") != intended_cursor and not (
            _legacy_terminal_cursor_allowed(report)
            and payload.get("cursor") == event.sequence - 1
        ):
            raise RuntimeError("live model terminal cursor differs from its intent")
        for field in (
            "reservation",
            "input",
            "manifest",
            "policy_version",
            "token_counter",
        ):
            if payload.get(field) != intended.payload.get(field):
                raise RuntimeError(f"live model {field} differs from its intent")
        visible_input = payload.get("input")
        if not isinstance(visible_input, str) or not visible_input:
            raise RuntimeError("model terminal event lacks visible input")
        try:
            request = parse_json(visible_input)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("model terminal input is not valid JSON") from exc
        if (
            not isinstance(request, dict)
            or not request.get("actions")
            or not request.get("result_schemas")
        ):
            raise RuntimeError("model request omitted visible schemas")
        attempts = payload.get("attempts")
        usage = _mapping(payload.get("usage"))
        model_manifest = _mapping(payload.get("manifest"))
        if (
            model_manifest.get("reserved_output_tokens") != MAX_OUTPUT_TOKENS
            or model_manifest.get("token_budget") != CONTEXT_WINDOW_TOKENS
        ):
            raise RuntimeError("model context manifest has inconsistent budgets")
        if not isinstance(attempts, (tuple, list)) or not attempts:
            raise RuntimeError("model terminal event lacks attempts")
        if usage.get("model_calls") != len(attempts):
            raise RuntimeError("model call usage does not reconcile to attempts")
        if any(
            not isinstance(_mapping(item).get("finish_reason"), str)
            for item in attempts
        ):
            raise RuntimeError("model attempt lacks finish reason")
        for field in ("input_tokens", "output_tokens", "duration_ms"):
            if not isinstance(usage.get(field), int) or usage[field] < 0:
                raise RuntimeError(f"model usage {field} is not complete")
        if event.kind == "model_decided" and not isinstance(payload.get("output"), str):
            raise RuntimeError("model decision lacks visible output")
    action_events = [event for event in events if event.kind == "action_intended"]
    receipt_events = [event for event in events if event.kind == "receipt_received"]
    if not action_events or len(action_events) != len(receipt_events):
        raise RuntimeError("action and receipt streams do not reconcile")
    if (
        _action_names(events).count("write") != 1
        or _action_names(events).count("test") != 1
    ):
        raise RuntimeError("live run did not exercise one write and one test")
    if not _accepted_evaluation(events):
        raise RuntimeError("live run has no accepted external evaluation")
    run = _mapping(_mapping(report).get("run"))
    counters = derive_counters(events)
    if _jsonable(counters) != run.get("session_counters"):
        raise RuntimeError("report Session.counters differs from replay")
    if counters.model_calls > MAX_MODEL_CALLS:
        raise RuntimeError("live model-call budget exceeded")
    if (
        counters.input_tokens is None
        or counters.output_tokens is None
        or counters.duration_ms is None
    ):
        raise RuntimeError("live model usage is not fully reconciled")


def _validate_model_cursor(event: Event, *, is_intent: bool) -> None:
    """Check the durable cursor relationship around one model call."""
    payload = event.payload
    cursor = payload.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int):
        raise RuntimeError("model event cursor does not match its durable position")
    if is_intent and cursor != event.sequence - 1:
        raise RuntimeError("model intent cursor does not match its durable position")
    manifest = _mapping(payload.get("manifest"))
    context_cursor = manifest.get("journal_cursor")
    if isinstance(context_cursor, bool) or not isinstance(context_cursor, int):
        raise RuntimeError("model event context cursor is invalid")
    if is_intent and context_cursor != cursor - 1:
        raise RuntimeError("model intent context cursor is not adjacent")
    if not is_intent and context_cursor != cursor - 1 and context_cursor != cursor - 2:
        raise RuntimeError("model terminal context cursor is invalid")


def _legacy_terminal_cursor_allowed(report: object) -> bool:
    """Recognize the pinned pre-correction bundle cursor shape explicitly."""
    source = _mapping(_mapping(report).get("source"))
    return source.get("git_revision") in _LEGACY_TERMINAL_CURSOR_REVISIONS


def _model_attempt_facts(events: Sequence[Event]) -> list[dict[str, object]]:
    """Return report attempt rows directly from durable model terminal events."""
    terminal_kinds = {"model_decided", "model_malformed"}
    fields = (
        "request_id",
        "result_kind",
        "finish_reason",
        "error",
        "attempts",
        "usage",
    )
    return [
        {
            "sequence": event.sequence,
            "kind": event.kind,
            **{field: event.payload.get(field) for field in fields},
        }
        for event in events
        if event.kind in terminal_kinds
    ]


def _validate_report_against_events(report: object, events: Sequence[Event]) -> None:
    """Recompute report-critical claims from the canonical journal projection."""
    mapping = _mapping(report)
    run = _mapping(mapping.get("run"))
    state = replay(events)
    derived = derive_counters(events)
    _require_equal(
        "report journal cursor", run.get("journal_cursor"), events[-1].sequence
    )
    _require_equal("report status", run.get("status"), state.status.value)
    _require_equal(
        "report accepted status",
        run.get("accepted"),
        state.status is SessionStatus.COMPLETED,
    )
    _require_equal(
        "report projection", run.get("projection_sha256"), _projection_digest(state)
    )
    _require_equal("report counters", run.get("counters"), _jsonable(derived))
    _require_equal(
        "report session counters", run.get("session_counters"), _jsonable(derived)
    )
    _require_equal(
        "report manifest", run.get("manifest"), _jsonable(derive_manifest(events))
    )
    _require_equal(
        "report accepted lineage",
        run.get("accepted_lineage"),
        [_jsonable(item) for item in state.accepted_lineage],
    )
    _require_equal(
        "report action names", run.get("action_names"), _action_names(events)
    )
    _require_equal(
        "report action receipts", run.get("action_receipts"), _action_receipts(events)
    )
    _require_equal(
        "report evaluation receipts",
        run.get("evaluation_receipts"),
        _evaluation_receipts(events),
    )
    _require_equal(
        "report unknown usage",
        run.get("unknown_usage_operation_ids"),
        list(state.ledger.unknown_usage_operation_ids),
    )
    _require_equal(
        "report unknown cost",
        run.get("unknown_cost_operation_ids"),
        list(state.ledger.unknown_cost_operation_ids),
    )
    expected_hashes = {
        str(event.sequence): _sha256_text(canonical_json(event)) for event in events
    }
    _require_equal("report event hashes", mapping.get("event_hashes"), expected_hashes)
    claims = _mapping(mapping.get("acceptance_claims"))
    _require_equal(
        "report external acceptance",
        claims.get("external_evaluator_accepted"),
        _accepted_evaluation(events),
    )
    _require_equal(
        "report lineage promotion",
        claims.get("lineage_promoted"),
        any(event.kind == "lineage_promoted" for event in events),
    )
    _require_equal("report evidence ID", mapping.get("evidence_id"), BUNDLE_ID)
    _require_equal("report package version", mapping.get("package_version"), "0.2.0")
    _require_equal(
        "report model configuration",
        mapping.get("configuration"),
        _jsonable(CONFIGURATION),
    )
    _require_equal(
        "report bound model configuration",
        mapping.get("model_configuration_bound"),
        _first_payload(events, "model_configuration_bound", "configuration"),
    )
    _require_equal("report session ID", run.get("session_id"), SESSION_ID)
    _require_equal("report task ID", run.get("task_id"), TASK_ID)
    _require_equal(
        "report adapter model calls",
        run.get("model_calls_observed_by_adapter"),
        derived.model_calls,
    )
    _require_equal(
        "report attempts",
        run.get("attempts"),
        _jsonable(_model_attempt_facts(events)),
    )
    _require_equal(
        "report provider rejection",
        claims.get("provider_rejected"),
        any(event.kind == "model_malformed" for event in events),
    )


def _build_bundle_manifest(
    output: Path, source: Mapping[str, object], events: Sequence[Event]
) -> dict[str, object]:
    """Hash every retained file except this recursively self-referential manifest."""
    files: dict[str, object] = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            relative = path.relative_to(output).as_posix()
            files[relative] = {
                "sha256": _sha256_path(path),
                "size": path.stat().st_size,
            }
    return {
        "manifest_version": 1,
        "evidence_id": BUNDLE_ID,
        "scope": "unscripted live LongCat CompatibleModel software-fixture acceptance",
        "source": dict(source),
        "package_version": "0.2.0",
        "configuration_digest": _configuration_digest(),
        "journal": _journal_facts(output / "session.sqlite3", events),
        "artifacts": _artifact_facts(output / "artifacts", events),
        "derived_files": {
            "report": "report.json",
            "transcript": "transcript.json",
            "context_manifests": "context-manifests.json",
            "fresh_process_reopen": "fresh-process-reopen.json",
            "reproducer": "reproduce.py",
        },
        "files": files,
        "self_hash": "omitted to avoid recursive manifest hashing",
    }


def _validate_bundle_manifest(bundle: Path, manifest: object) -> None:
    """Validate exact retained-file set and every raw-byte digest."""
    _assert_no_reparse_components(bundle)
    mapping = _mapping(manifest)
    files = mapping.get("files")
    if not isinstance(files, Mapping) or not files:
        raise RuntimeError("manifest files are invalid")
    declared: set[str] = set()
    for raw_path, raw_facts in files.items():
        relative = _safe_relative_path(raw_path, "manifest file")
        if relative == "manifest.json":
            raise RuntimeError("manifest must not declare itself")
        path = _safe_bundle_path(bundle, relative, "manifest file")
        facts = _mapping(raw_facts)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"manifest file is missing: {relative}")
        if not isinstance(facts.get("size"), int) or not _is_sha256(
            facts.get("sha256")
        ):
            raise RuntimeError(f"manifest facts are invalid: {relative}")
        if (
            path.stat().st_size != facts["size"]
            or _sha256_path(path) != facts["sha256"]
        ):
            raise RuntimeError(f"manifest hash mismatch: {relative}")
        declared.add(relative)
    actual = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "manifest.json"
    }
    if actual != declared:
        raise RuntimeError("manifest file set mismatch")
    for name, raw_path in _mapping(mapping.get("derived_files")).items():
        relative = _safe_relative_path(raw_path, f"derived file {name}")
        if relative not in declared:
            raise RuntimeError(f"derived file is not retained: {relative}")


def _validate_source_pin(bundle: Path, source_root: Path, manifest: object) -> None:
    """Prove the bundled reproducer is the pinned Git revision's generator."""
    source = _mapping(_mapping(manifest).get("source"))
    revision = source.get("git_revision")
    digest = source.get("generator_sha256")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("source Git revision is invalid")
    if source.get("git_dirty") is not False or not _is_sha256(digest):
        raise RuntimeError("source is not a clean pinned revision")
    source_root = source_root.resolve()
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"pinned source revision is unavailable: {revision}")
    generated = subprocess.run(
        ["git", "show", f"{revision}:tools/phase2_live_software_evidence.py"],
        cwd=source_root,
        capture_output=True,
        check=False,
    )
    if generated.returncode != 0:
        raise RuntimeError("pinned source revision lacks the live evidence generator")
    reproduce = bundle / "reproduce.py"
    if _normalized_sha256(cast(bytes, generated.stdout)) != _normalized_sha256(
        reproduce.read_bytes()
    ):
        raise RuntimeError("bundled reproducer differs from pinned source")
    if digest != _normalized_sha256(reproduce.read_bytes()):
        raise RuntimeError("bundled reproducer digest differs from manifest")


def _journal_facts(path: Path, events: Sequence[Event]) -> dict[str, object]:
    """Return SQLite, event-stream, and replay facts."""
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
    """Verify all artifact references reachable from the journal."""
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
        "journal_pointer": (path / ".journals").read_text(encoding="utf-8").strip(),
        "reachable_artifacts": checked,
        "inventory": [
            {"digest": digest, "size": size} for digest, size in store.inventory()
        ],
    }


def _validate_journal_facts(facts: Mapping[str, object]) -> None:
    """Require durable SQLite integrity and no mutable sidecars."""
    if facts.get("integrity_check") != "ok" or facts.get("foreign_key_check") != []:
        raise RuntimeError("SQLite integrity checks failed")
    if facts.get("wal_sidecars") != []:
        raise RuntimeError("SQLite sidecar is present")


def _validate_artifact_facts(
    bundle: Path, journal_path: Path, artifacts_path: Path, facts: Mapping[str, object]
) -> None:
    """Require relative journal pointers and canonical artifact layout."""
    _assert_no_reparse_components(bundle)
    relative = _safe_relative_path(
        facts.get("journal_pointer"), "artifact journal pointer", allow_parent=True
    )
    pointer = _safe_bundle_path(
        bundle, f"{artifacts_path.name}/{relative}", "artifact journal pointer"
    ).resolve()
    if pointer != journal_path.resolve() or not pointer.is_file():
        raise RuntimeError("artifact journal pointer is invalid")
    try:
        pointer.relative_to(bundle.resolve())
    except ValueError as exc:
        raise RuntimeError("artifact journal pointer escapes bundle") from exc
    for path in artifacts_path.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.name == ".journals":
            continue
        relative_path = path.relative_to(artifacts_path).as_posix()
        if len(PurePosixPath(relative_path).parts) != 2:
            raise RuntimeError(f"unexpected artifact path: {relative_path}")


def _context_manifests(events: Sequence[Event]) -> list[dict[str, object]]:
    """Retain every deterministic context manifest from the journal."""
    return [
        {"sequence": event.sequence, "manifest": event.payload.get("manifest")}
        for event in events
        if event.kind == "context_assembled"
    ]


def _transcript(events: Sequence[Event]) -> list[dict[str, object]]:
    """Retain concise durable event details without credential material."""
    return [
        {
            "sequence": event.sequence,
            "kind": event.kind,
            "payload": _jsonable(event.payload),
        }
        for event in events
    ]


def _action_receipts(events: Sequence[Event]) -> list[dict[str, object]]:
    """Return the durable action/receipt correlation stream."""
    return [
        {
            "sequence": event.sequence,
            "proposal_id": _mapping(event.payload.get("receipt")).get("proposal_id"),
            "status": _mapping(event.payload.get("receipt")).get("status"),
        }
        for event in events
        if event.kind == "receipt_received"
    ]


def _evaluation_receipts(events: Sequence[Event]) -> list[dict[str, object]]:
    """Return durable external evaluator dispositions."""
    return [
        {
            "sequence": event.sequence,
            "accepted": _mapping(event.payload.get("receipt")).get("accepted"),
            "disposition": event.payload.get("disposition"),
        }
        for event in events
        if event.kind == "evaluation_received"
    ]


def _accepted_evaluation(events: Sequence[Event]) -> bool:
    return any(
        event.kind == "evaluation_received"
        and _mapping(event.payload.get("receipt")).get("accepted") is True
        for event in events
    )


def _action_names(events: Sequence[Event]) -> list[str]:
    return [
        str(_mapping(event.payload.get("proposal")).get("action_name"))
        for event in events
        if event.kind == "action_intended"
    ]


def _run_identity() -> RunIdentity:
    return RunIdentity(
        package_version="0.2.0",
        configuration_digest=_configuration_digest(),
        runtime=ComponentIdentity("software-fixture-runtime", "v1"),
        evaluator=ComponentIdentity("software-fixture-evaluator", "v1"),
    )


def _configuration_digest() -> str:
    return _sha256_text(canonical_json(CONFIGURATION))


def _credential() -> str:
    """Read the explicitly authorized Windows user-scope credential."""
    process_value = os.environ.get(CREDENTIAL_ENV)
    if process_value:
        return process_value
    if os.name != "nt":
        raise RuntimeError(f"{CREDENTIAL_ENV} is not available")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Environment]::GetEnvironmentVariable('LONGCAT_API_KEY','User')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not value:
        raise RuntimeError(f"{CREDENTIAL_ENV} is not available in Windows user scope")
    return value


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
    generator = source_root.resolve() / "tools" / "phase2_live_software_evidence.py"
    if not generator.is_file():
        raise RuntimeError(f"live evidence generator is missing: {generator}")
    return {
        "git_revision": revision,
        "git_dirty": dirty,
        "generator_sha256": _normalized_sha256(generator.read_bytes()),
    }


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


def _all_events(journal: SqliteJournal, session_id: str) -> tuple[Event, ...]:
    events: list[Event] = []
    cursor = 0
    while True:
        page = journal.events(session_id, after_sequence=cursor, limit=100)
        if not page:
            return tuple(events)
        events.extend(page)
        cursor = page[-1].sequence


def _first_payload(events: Sequence[Event], kind: str, field: str) -> object:
    return next(event.payload.get(field) for event in events if event.kind == kind)


def _artifact_refs(value: object) -> set[ArtifactRef]:
    found: set[ArtifactRef] = set()
    if isinstance(value, Mapping):
        try:
            if all(
                isinstance(value.get(name), expected)
                for name, expected in (
                    ("digest", str),
                    ("size", int),
                    ("media_type", str),
                    ("provenance", str),
                )
            ):
                found.add(
                    ArtifactRef(
                        cast(str, value["digest"]),
                        cast(int, value["size"]),
                        cast(str, value["media_type"]),
                        cast(str, value["provenance"]),
                    )
                )
        except (TypeError, ValueError):
            pass
        for item in value.values():
            found.update(_artifact_refs(item))
    elif isinstance(value, (tuple, list)):
        for item in value:
            found.update(_artifact_refs(item))
    return found


def _sqlite_integrity(path: Path) -> tuple[str, list[list[object]]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        return integrity, foreign_keys
    finally:
        connection.close()


def _view_matches_state(view: object, state: SessionState) -> bool:
    return (
        getattr(view, "status", None) is state.status
        and getattr(getattr(view, "task", None), "objective", None)
        == state.task.objective
        and getattr(getattr(view, "task", None), "objective_version", None)
        == state.task.objective_version
        and getattr(getattr(view, "lineage_head", None), "node_id", None)
        == state.lineage_head.node_id
        and getattr(view, "pending_proposal_id", None) == state.pending_proposal_id
        and getattr(view, "wait_reason", None) == state.wait_reason
    )


def _validate_current_projection(
    view: SessionView, state: SessionState, cursor: int, label: str
) -> None:
    """Reject a reopened public view that differs from canonical replay."""
    if view.cursor != cursor or not _view_matches_state(view, state):
        raise RuntimeError(f"{label} current projection mismatch")


def _artifact_facts_equal(value: object) -> Mapping[str, object]:
    return _mapping(value)


def _require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} mismatch")


def _validate_report_fresh_process(
    report_claim: object,
    fresh: object,
    source_revision: object,
    counters: object,
) -> None:
    """Keep legacy counter omissions pinned and verify them from journal replay."""
    label = "report fresh-process verification"
    if source_revision not in _LEGACY_TERMINAL_CURSOR_REVISIONS:
        _require_equal(label, report_claim, fresh)
        return
    claim = dict(_mapping(report_claim))
    claim_run = dict(_mapping(claim.get("run")))
    claim_counters = dict(_mapping(claim_run.get("counters")))
    fresh_run = _mapping(_mapping(fresh).get("run"))
    fresh_counters = _mapping(fresh_run.get("counters"))
    replay_counters = _mapping(_jsonable(counters))
    legacy_fields = {
        "supervisor_calls",
        "conservative_supervisor_calls",
        "supervisor_input_tokens",
        "supervisor_output_tokens",
    }
    missing = set(fresh_counters) - set(claim_counters)
    if not missing <= legacy_fields:
        raise RuntimeError(f"{label} mismatch")
    for field in missing:
        if replay_counters.get(field) != fresh_counters.get(field):
            raise RuntimeError(f"{label} mismatch")
        claim_counters[field] = replay_counters[field]
    claim_run["counters"] = claim_counters
    claim["run"] = claim_run
    _require_equal(label, claim, fresh)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_relative_path(
    value: object, label: str, *, allow_parent: bool = False
) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError(f"{label} must be a canonical relative POSIX path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    forbidden = {"", "."} if allow_parent else {"", ".", ".."}
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in forbidden for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise RuntimeError(f"{label} must be a canonical relative POSIX path")
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


def _projection_digest(state: SessionState) -> str:
    # Keep the report digest compatible with retained pre-correction bundles.
    # Event hashes and replay still cover the newer model-operation projection.
    return _legacy_projection_digest(state)


def _jsonable(value: object) -> object:
    return json.loads(canonical_json(value))


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(
        (
            json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    )


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_bytes(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n")


def _normalized_sha256(value: bytes) -> str:
    return hashlib.sha256(_normalized_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "<redacted-credential>")


def _assert_secret_absent(root: Path, secret: str) -> None:
    for path in root.rglob("*"):
        if path.is_file() and secret.encode("utf-8") in path.read_bytes():
            raise RuntimeError(f"credential material was retained in {path.name}")


if __name__ == "__main__":
    raise SystemExit(main())
