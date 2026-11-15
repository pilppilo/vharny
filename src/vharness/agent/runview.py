"""Read-only review of one operator run directory.

The projector reads ``run.json`` and ``session.sqlite3``, replays the journal,
and reports status, settings, model calls, action rationales, context-manifest
omissions, and artifact digests. It does not open a live session, the runtime
store, or artifact bodies, and it does not print the rendered model prompt.
"""

# The screen and the JSON projection share one module so the fields cannot drift.
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from .codec import parse_json_object
from .errors import ContractError
from .evidence import counters as derive_counters
from .journal import SqliteJournal
from .models import (
    OPERATOR_ANSWER_WAIT,
    PROVIDER_RECOVERY_WAIT,
    Event,
    JsonValue,
    SessionStatus,
)
from .replay import replay
from .run_settings import RunManifestRecord
from .runview_cli import run_command

# RunDirectory.manifest_path and journal_path. The string is local so this
# reviewer does not import the POSIX coordinator.
_MANIFEST_NAME = "run.json"
_JOURNAL_NAME = "session.sqlite3"
_GATE_LINE = re.compile(r"^(PASS|FAIL)\s+(\S+):\s*(.*)$")
_OBJECTIVE_EXCERPT = 500
_TEXT_LIMIT = 160
_DEADLINE_KEYS = ("deadline", "retry_at", "next_attempt_at")
_MARK_FIELDS = (
    "cause",
    "call",
    "decision",
    "reason",
    "failure_class",
    "result_kind",
    "field",
    "disposition",
    "stage",
    "detail",
    "question_id",
    "request_id",
)
_MARK_KINDS = frozenset(
    {
        "supervision_triggered",
        "guidance_accepted",
        "guidance_rejected",
        "guidance_response_rejected",
        "guidance_suppressed",
        "model_repaired",
        "model_result_suppressed",
        "model_application_rejected",
        "checkpoint_suppressed",
        "context_overflow",
        "evaluation_intended",
        "evaluation_received",
        "operator_escalated",
        "question_recorded",
        "provider_recovery_waiting",
    }
)
_REJECTION_DISPOSITIONS = frozenset({"rejected", "invalid", "hard_constraint_failed"})
_RECORD_END_KINDS = frozenset(
    {"model_malformed", "supervisor_failed", "context_overflow"}
)
_OPEN_STATUSES = frozenset(
    {
        SessionStatus.CREATED,
        SessionStatus.RUNNING,
        SessionStatus.PAUSED,
        SessionStatus.STOPPING,
    }
)
# A runner_exited tail leaves these durable statuses unchanged. The review
# still reports the dead run as failed, matching the operator status line.
_RUNNER_EXIT_LIVE = frozenset(
    {
        SessionStatus.CREATED,
        SessionStatus.RUNNING,
        SessionStatus.WAITING,
    }
)


def review(events: tuple[Event, ...]) -> dict[str, object]:
    """Project one validated event stream into the Phase 1 review facts."""
    if not events:
        raise ContractError("run review requires a journal event stream")
    state = replay(events)
    counted = derive_counters(events)
    stopped = _why_stopped(state.status, state.wait_reason, events)
    calls = _calls(events)
    actions = _actions(events, calls)
    compared = _compare_row(events, counted, stopped)
    return {
        "status": _reported_status(state.status, events),
        "wait_reason": state.wait_reason,
        "why_stopped": stopped,
        "waits": _waits(events),
        "supervision_policy": _supervision_policy(events),
        "calls": calls,
        "actions": actions,
        "marks": _marks(events),
        "counters": _counter_payload(counted),
        "compare": compared,
        "settings": None,
        "gate_log": None,
        "directory": None,
    }


def show_run(root: str | Path, gate_log: str | Path | None = None) -> dict[str, object]:
    """Load one run directory and return its review projection."""
    directory = Path(root)
    manifest = _read_manifest(directory)
    events = load_events(directory / _JOURNAL_NAME, manifest.session_id)
    view = review(events)
    view["directory"] = str(directory)
    view["settings"] = _settings(manifest)
    compared = view["compare"]
    if isinstance(compared, dict):
        compared["directory"] = str(directory)
    view["gate_log"] = None if gate_log is None else parse_gate_log(gate_log)
    return view


def compare_runs(roots: Sequence[str | Path]) -> dict[str, object]:
    """Return one compare row per run directory from the same projector."""
    if not roots:
        raise ContractError("compare requires at least one run directory")
    rows: list[dict[str, object]] = []
    for root in roots:
        view = show_run(root)
        compared = view["compare"]
        if not isinstance(compared, dict):
            raise ContractError("compare row was not projected")
        rows.append(dict(compared))
    return {"runs": rows}


def load_events(path: Path, session_id: str) -> tuple[Event, ...]:
    """Read one session's ordered events from a sqlite journal."""
    journal = SqliteJournal(path, read_only=True)
    try:
        if session_id not in journal.session_ids():
            raise ContractError(f"journal has no session {session_id}")
        collected: list[Event] = []
        cursor = 0
        while True:
            window = journal.events(session_id, after_sequence=cursor, limit=1000)
            if not window:
                break
            collected.extend(window)
            cursor = window[-1].sequence
    finally:
        journal.close()
    if not collected:
        raise ContractError(f"session {session_id} has no events")
    return tuple(collected)


def parse_gate_log(path: str | Path) -> dict[str, object]:
    """Count an external preflight log. It is not an in-run tooling reason."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"could not read gate log {source}: {exc}") from exc
    passed = 0
    failures: list[dict[str, str]] = []
    for line in text.splitlines():
        match = _GATE_LINE.match(line.strip())
        if match is None:
            continue
        kind, check, detail = match.groups()
        if kind == "PASS":
            passed += 1
            continue
        failures.append({"check": check, "detail": detail})
    return {
        "source": "external",
        "path": str(source),
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
    }


def format_show(view: Mapping[str, object]) -> str:
    """Render one review as a single text screen."""
    lines = [
        f"directory: {_or_none(view.get('directory'))}",
        f"status: {view.get('status')}",
        f"wait_reason: {_or_none(view.get('wait_reason'))}",
        f"supervision_policy: {view.get('supervision_policy')}",
        _format_stop(view.get("why_stopped")),
        _format_settings(view.get("settings")),
        _format_gate(view.get("gate_log")),
        _format_counters(view.get("counters")),
        "calls:",
        *_format_calls(view.get("calls")),
        "actions:",
        *_format_actions(view.get("actions")),
        "marks:",
        *_format_marks(view.get("marks")),
        "waits:",
        *_format_waits(view.get("waits")),
    ]
    return "\n".join(lines) + "\n"


def format_compare(report: Mapping[str, object]) -> str:
    """Render compare rows as a compact text table."""
    runs = report.get("runs")
    header = (
        "run calls actions tokens suppressions plan eval "
        "supervision unknown stop_class stop"
    )
    if not isinstance(runs, list) or not runs:
        return header + "\n"
    lines = [header]
    for row in runs:
        if isinstance(row, Mapping):
            lines.append(_format_compare_row(row))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``runview`` command line and return a process status."""
    return run_command(_parser(), argv, _execute)


def _execute(args: argparse.Namespace) -> None:
    """Render the parsed ``runview`` command to standard output."""
    if args.command == "show":
        view = show_run(args.run_dir, args.gate_log)
        rendered = (
            json.dumps(view, indent=2, sort_keys=True) + "\n"
            if args.as_json
            else format_show(view)
        )
    else:
        report = compare_runs(args.run_dirs)
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.as_json
            else format_compare(report)
        )
    sys.stdout.write(rendered)


def _parser() -> argparse.ArgumentParser:
    """Build the runview argument parser."""
    parser = argparse.ArgumentParser(
        prog="runview",
        description="Review an operator run from its journal and run.json.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print one run review")
    show.add_argument("run_dir")
    show.add_argument("--json", action="store_true", dest="as_json")
    show.add_argument("--gate-log", dest="gate_log")
    compare = commands.add_parser("compare", help="compare run directories")
    compare.add_argument("run_dirs", nargs="+")
    compare.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _reported_status(status: SessionStatus, events: tuple[Event, ...]) -> str:
    """Return the status a finished runner exit presents to the operator.

    ``runner_exited`` does not change the durable projection, so resume can
    still reopen it. When that event is the journal tail and the projection
    is still live, the run is finished and the review says failed.
    """
    if events[-1].kind == "runner_exited" and status in _RUNNER_EXIT_LIVE:
        return SessionStatus.FAILED.value
    return status.value


def _read_manifest(directory: Path) -> RunManifestRecord:
    """Decode ``run.json`` the way ``RunDirectory.open`` does."""
    path = directory / _MANIFEST_NAME
    if not path.is_file():
        raise ContractError("run directory has no run manifest")
    try:
        payload = parse_json_object(path.read_bytes())
    except OSError as exc:
        raise ContractError(f"could not read run manifest {path}: {exc}") from exc
    return RunManifestRecord.from_payload(payload)


def _settings(manifest: RunManifestRecord) -> dict[str, object]:
    """Return the non-secret settings block from a run manifest record."""
    payload = manifest.settings.to_payload()
    objective = payload.get("objective")
    text = objective if isinstance(objective, str) else ""
    excerpt = (
        text if len(text) <= _OBJECTIVE_EXCERPT else text[:_OBJECTIVE_EXCERPT] + "..."
    )
    return {
        "objective": text,
        "objective_excerpt": excerpt,
        "workspace": payload.get("workspace"),
        "target": payload.get("target"),
        "model": payload.get("model"),
        "base_url": payload.get("base_url"),
        "api_key_env": payload.get("api_key_env"),
        "shell": payload.get("shell"),
        "max_actions": payload.get("max_actions"),
        "max_model_calls": payload.get("max_model_calls"),
        "max_evaluations": payload.get("max_evaluations"),
        "max_steps": payload.get("max_steps"),
        "token_budget": payload.get("token_budget"),
        "max_output_tokens": payload.get("max_output_tokens"),
        "repair_budget": payload.get("repair_budget"),
        "check_command": payload.get("check_command"),
        "evaluation_mode": manifest.evaluation_mode,
        "transport_recovery": payload.get("transport_recovery"),
        "package_version": manifest.package_version,
        "session_id": manifest.session_id,
    }


def _supervision_policy(events: tuple[Event, ...]) -> str:
    """Return the recorded monitor policy version, or an explicit absence."""
    for event in events:
        if event.kind != "supervision_enabled":
            continue
        policy = event.payload.get("policy")
        if isinstance(policy, Mapping):
            version = policy.get("version")
            if isinstance(version, str) and version.strip():
                return version
        return "not recorded"
    return "not recorded"


def _calls(events: tuple[Event, ...]) -> list[dict[str, object]]:
    """Pair each model or supervisor intent with its terminal event."""
    pending_main: dict[str, Event] = {}
    pending_supervisor: dict[str, Event] = {}
    rows: list[dict[str, object]] = []
    for event in events:
        request_id = _request_id(event)
        if event.kind == "model_intended" and request_id is not None:
            pending_main[request_id] = event
        elif event.kind in {"model_decided", "model_malformed"}:
            intent = pending_main.pop(request_id, None) if request_id else None
            rows.append(_main_call(intent, event))
        elif event.kind == "supervisor_intended" and request_id is not None:
            pending_supervisor[request_id] = event
        elif event.kind in {"guidance_received", "supervisor_failed"}:
            intent = pending_supervisor.pop(request_id, None) if request_id else None
            rows.append(_supervisor_call(intent, event))
    for intent in pending_main.values():
        rows.append(_main_call(intent, None))
    for intent in pending_supervisor.values():
        rows.append(_supervisor_call(intent, None))
    rows.sort(key=lambda row: _sequence_key(row.get("sequence")))
    return rows


def _main_call(intent: Event | None, terminal: Event | None) -> dict[str, object]:
    """Build one main-model call row without the rendered prompt."""
    anchor = intent or terminal
    if anchor is None:
        raise ContractError("model call row requires an event")
    payload = terminal.payload if terminal is not None else {}
    outcome = "open"
    if terminal is not None and terminal.kind == "model_decided":
        outcome = "decided"
    elif terminal is not None:
        outcome = "malformed"
    manifest_source = intent.payload if intent is not None else payload
    row = _call_identity(anchor, "main", outcome, payload)
    row["result_kind"] = _string(payload, "result_kind")
    row["context_manifest"] = _manifest_summary(manifest_source)
    row["diagnosis"] = None
    return row


def _supervisor_call(intent: Event | None, terminal: Event | None) -> dict[str, object]:
    """Build one supervisor call row. Supervisor intents have no manifest."""
    anchor = intent or terminal
    if anchor is None:
        raise ContractError("supervisor call row requires an event")
    payload = terminal.payload if terminal is not None else {}
    outcome = "open"
    if terminal is not None and terminal.kind == "guidance_received":
        outcome = "guidance"
    elif terminal is not None:
        outcome = "failed"
    row = _call_identity(anchor, "supervisor", outcome, payload)
    row["result_kind"] = None
    row["context_manifest"] = None
    row["diagnosis"] = _diagnosis(payload)
    return row


def _call_identity(
    anchor: Event, role: str, outcome: str, payload: Mapping[str, object]
) -> dict[str, object]:
    """Return the shared call fields. Prompt text is never copied."""
    return {
        "sequence": anchor.sequence,
        "recorded_at": anchor.recorded_at,
        "role": role,
        "request_id": _request_id(anchor) or _string(payload, "request_id"),
        "outcome": outcome,
        "input_tokens": _usage_int(payload, "input_tokens"),
        "output_tokens": _usage_int(payload, "output_tokens"),
        "reasoning_tokens": _attempt_int(payload, "reasoning_tokens"),
        "duration_ms": _optional_int(payload.get("duration_ms")),
        "failure_class": _string(payload, "failure_class"),
        "detail": _snippet(_string(payload, "error")),
    }


def _attempt_int(payload: Mapping[str, object], name: str) -> int | None:
    """Read one bounded integer from the last recorded model attempt."""
    attempts = payload.get("attempts")
    if not isinstance(attempts, (list, tuple)) or not attempts:
        return None
    last = attempts[-1]
    if not isinstance(last, Mapping):
        return None
    value = last.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _manifest_summary(payload: Mapping[str, object]) -> dict[str, object] | None:
    """Summarize included and omitted context ids from a call record."""
    manifest = payload.get("manifest")
    if not isinstance(manifest, Mapping):
        return None
    included = _pairs(manifest.get("included"))
    omitted = _pairs(manifest.get("omitted"))
    reasons: dict[str, int] = {}
    for _item_id, reason in omitted:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "included_count": len(included),
        "omitted_count": len(omitted),
        "omission_reasons": reasons,
        "included": [[item_id, band] for item_id, band in included],
        "omitted": [[item_id, reason] for item_id, reason in omitted],
    }


def _actions(
    events: tuple[Event, ...], calls: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Join each proposal to its receipt, rationale, and artifact digest."""
    main_calls = [
        (_sequence_key(call.get("sequence")), _string_value(call.get("request_id")))
        for call in calls
        if call.get("role") == "main"
    ]
    rows: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for event in events:
        if event.kind == "action_intended":
            row = _action_row(event, main_calls)
            proposal_id = _string_value(row.get("proposal_id"))
            if proposal_id is None:
                continue
            rows[proposal_id] = row
            order.append(proposal_id)
        elif event.kind == "receipt_received":
            _attach_receipt(rows, event)
    return [rows[proposal_id] for proposal_id in order if proposal_id in rows]


def _action_row(
    event: Event, main_calls: Sequence[tuple[int, str | None]]
) -> dict[str, object]:
    """Build one action row from an action intent."""
    proposal = event.payload.get("proposal")
    if not isinstance(proposal, Mapping):
        proposal = {}
    proposal_id = _string(proposal, "proposal_id")
    return {
        "sequence": event.sequence,
        "proposal_id": proposal_id,
        "request_id": _owning_request(event.sequence, main_calls),
        "action_name": _string(proposal, "action_name"),
        "arguments": _short_arguments(proposal.get("arguments")),
        "rationale": _string(proposal, "rationale"),
        "receipt_status": None,
        "receipt_sequence": None,
        "error": None,
        "artifact_digest": None,
    }


def _attach_receipt(rows: dict[str, dict[str, object]], event: Event) -> None:
    """Fill receipt status, error, and artifact digest onto a known proposal."""
    receipt = event.payload.get("receipt")
    if not isinstance(receipt, Mapping):
        return
    proposal_id = _string(receipt, "proposal_id")
    if proposal_id is None or proposal_id not in rows:
        return
    row = rows[proposal_id]
    row["receipt_status"] = _string(receipt, "status")
    row["receipt_sequence"] = event.sequence
    row["error"] = _snippet(_string(receipt, "error"))
    row["artifact_digest"] = _artifact_digest(receipt.get("raw_artifact"))


def _owning_request(
    sequence: int, main_calls: Sequence[tuple[int, str | None]]
) -> str | None:
    """Return the latest main call that started before this action."""
    owner: str | None = None
    for call_sequence, request_id in main_calls:
        if call_sequence < sequence:
            owner = request_id
    return owner


def _marks(events: tuple[Event, ...]) -> list[dict[str, object]]:
    """Collect in-run tooling marks. Gate logs are not marks."""
    rows: list[dict[str, object]] = []
    for event in events:
        if event.kind not in _MARK_KINDS:
            continue
        fields = {
            key: event.payload.get(key) for key in _MARK_FIELDS if key in event.payload
        }
        if event.kind == "context_overflow":
            fields.update(_overflow_fields(event.payload.get("overflow")))
        if event.kind == "question_recorded":
            fields["text"] = _snippet(_string(event.payload, "text"))
        rows.append(
            {
                "sequence": event.sequence,
                "recorded_at": event.recorded_at,
                "kind": event.kind,
                "fields": fields,
            }
        )
    return rows


def _overflow_fields(value: object) -> dict[str, JsonValue]:
    """Return the budget numbers from a context-overflow payload."""
    if not isinstance(value, Mapping):
        return {}
    fields: dict[str, JsonValue] = {}
    for key in ("token_budget", "tokens_required"):
        number = _optional_int(value.get(key))
        if number is not None:
            fields[key] = number
    missing = value.get("missing_item_ids")
    if isinstance(missing, list):
        fields["missing_count"] = len(missing)
    return fields


def _waits(events: tuple[Event, ...]) -> list[dict[str, object]]:
    """List durable waits, their age to the clearing event, and any deadline."""
    rows: list[dict[str, object]] = []
    for index, event in enumerate(events):
        if event.kind not in {
            "agent_waited",
            "question_recorded",
            "provider_recovery_waiting",
        }:
            continue
        closer = _wait_closer(events, index)
        rows.append(
            {
                "kind": event.kind,
                "sequence": event.sequence,
                "since": event.recorded_at,
                "reason": _wait_reason(event),
                "open": closer is None,
                "cleared_by": None if closer is None else closer.kind,
                "cleared_sequence": None if closer is None else closer.sequence,
                "age_seconds": _age_seconds(event, closer),
                "deadline": _deadline(event.payload),
            }
        )
    return rows


def _wait_closer(events: tuple[Event, ...], index: int) -> Event | None:
    """Return the later event that ends this wait, when one was journaled."""
    opened = events[index]
    question_id = _string(opened.payload, "question_id")
    for later in events[index + 1 :]:
        if opened.kind == "question_recorded":
            if _question_cleared(later, question_id) or later.kind in {
                "session_stopped",
                "objective_steered",
            }:
                return later
            continue
        if later.kind in {"session_resumed", "session_stopped", "objective_steered"}:
            return later
    return None


def _question_cleared(event: Event, question_id: str | None) -> bool:
    """Return whether this event answers the open operator question."""
    if event.kind != "question_answered" or question_id is None:
        return False
    return _string(event.payload, "question_id") == question_id


def _wait_reason(event: Event) -> str | None:
    """Return the recorded wait reason or provider-recovery detail."""
    if event.kind == "question_recorded":
        return _string(event.payload, "text")
    if event.kind == "provider_recovery_waiting":
        return _string(event.payload, "detail")
    return _string(event.payload, "reason")


def _deadline(payload: Mapping[str, object]) -> str:
    """Return a deadline only when the wait payload itself records one."""
    for key in _DEADLINE_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return "no deadline recorded"


def _why_stopped(
    status: SessionStatus, wait_reason: str | None, events: tuple[Event, ...]
) -> dict[str, object]:
    """Classify the stop from replay status and the tail fact."""
    closed = _closed_stop(status, wait_reason, events)
    if closed is not None:
        return closed
    tailed = _tail_stop(events[-1])
    if tailed is not None:
        return tailed
    return _open_stop(status, events)


def _closed_stop(
    status: SessionStatus, wait_reason: str | None, events: tuple[Event, ...]
) -> dict[str, object] | None:
    """Return a stop class that replay status already decides."""
    tail = events[-1]
    if status is SessionStatus.STOPPED:
        return _stop("operator_stop", _last(events, "session_stopped") or tail, None)
    if status is SessionStatus.WAITING and wait_reason == OPERATOR_ANSWER_WAIT:
        event = _last(events, "question_recorded") or tail
        return _stop("question_blocked", event, _string(event.payload, "text"))
    recovery = _last(events, "provider_recovery_waiting")
    if (
        status is SessionStatus.WAITING
        and wait_reason == PROVIDER_RECOVERY_WAIT
        and recovery is not None
    ):
        return _stop("provider_recovery", recovery, _string(recovery.payload, "detail"))
    return None


def _tail_stop(tail: Event) -> dict[str, object] | None:
    """Return a stop class decided by the last journal event."""
    if tail.kind == "provider_recovery_waiting":
        return _stop("provider_recovery", tail, _string(tail.payload, "detail"))
    if tail.kind == "runner_exited":
        reason = _string(tail.payload, "reason")
        kind = (
            "budget"
            if reason is not None and "budget" in reason.lower()
            else "runner_exit"
        )
        return _stop(kind, tail, reason)
    if _terminal_rejection(tail):
        return _stop("rejected", tail, _rejection_reason(tail))
    return None


def _open_stop(status: SessionStatus, events: tuple[Event, ...]) -> dict[str, object]:
    """Return the class for a session that has no terminal tail fact."""
    tail = events[-1]
    if status is SessionStatus.COMPLETED:
        return _stop("completed", _last(events, "lineage_promoted") or tail, None)
    if status is SessionStatus.FAILED:
        return _stop("failed", tail, None)
    if status is SessionStatus.WAITING:
        event = _last_wait(events) or tail
        return _stop("waiting", event, _wait_reason(event))
    if status in _OPEN_STATUSES:
        if tail.kind in _RECORD_END_KINDS:
            return _stop("open", tail, _record_end_reason(tail))
        return _stop("open", None, None)
    return _stop(status.value, tail, None)


def _record_end_reason(event: Event) -> str | None:
    """Return the safe detail on a journal that ends in a model or context failure."""
    if event.kind == "context_overflow":
        return "context overflow"
    return _string(event.payload, "failure_class") or _string(event.payload, "error")


def _stop(kind: str, event: Event | None, reason: str | None) -> dict[str, object]:
    """Return one stop fact tied to an event when the journal has one."""
    return {
        "stop_class": kind,
        "event_kind": None if event is None else event.kind,
        "sequence": None if event is None else event.sequence,
        "reason": reason,
    }


def _terminal_rejection(event: Event) -> bool:
    """Return whether the tail event is a completion or evaluation rejection."""
    if event.kind == "completion_rejected":
        return True
    if event.kind != "evaluation_received":
        return False
    disposition = _string(event.payload, "disposition")
    if disposition in _REJECTION_DISPOSITIONS:
        return True
    receipt = event.payload.get("receipt")
    return isinstance(receipt, Mapping) and receipt.get("accepted") is False


def _rejection_reason(event: Event) -> str | None:
    """Return the recorded rejection reason when the tail event has one."""
    reason = _string(event.payload, "reason")
    if reason is not None:
        return reason
    receipt = event.payload.get("receipt")
    if isinstance(receipt, Mapping):
        return _string(receipt, "comparison") or "evaluation rejected"
    return "evaluation rejected"


def _compare_row(
    events: tuple[Event, ...],
    counted: object,
    stopped: Mapping[str, object],
) -> dict[str, object]:
    """Derive the compare table from the same replay as the screen."""
    reason = stopped.get("reason")
    stop_class = stopped.get("stop_class")
    return {
        "calls": getattr(counted, "model_calls"),
        "actions": getattr(counted, "actions"),
        "input_tokens": getattr(counted, "input_tokens"),
        "output_tokens": getattr(counted, "output_tokens"),
        "suppressions": sum(
            event.kind in {"model_result_suppressed", "checkpoint_suppressed"}
            for event in events
        ),
        "plan_proposed": any(event.kind == "plan_recorded" for event in events),
        "evaluation_requested": any(
            event.kind == "evaluation_intended" for event in events
        ),
        "supervision_fires": sum(
            event.kind == "supervision_triggered" for event in events
        ),
        "unknown_outcomes": _unknown_outcomes(events),
        "stop_class": stop_class,
        "stop_reason": reason if isinstance(reason, str) and reason else stop_class,
    }


def _unknown_outcomes(events: tuple[Event, ...]) -> int:
    """Count indeterminate receipts and unknown reconciliations."""
    count = 0
    for event in events:
        if event.kind == "receipt_received":
            receipt = event.payload.get("receipt")
            if (
                isinstance(receipt, Mapping)
                and receipt.get("status") == "indeterminate"
            ):
                count += 1
        elif (
            event.kind == "operation_reconciled"
            and event.payload.get("result") == "unknown"
        ):
            count += 1
    return count


def _counter_payload(counted: object) -> dict[str, object]:
    """Copy evidence counters without the replay envelope object."""
    names = (
        "actions",
        "evaluations",
        "model_calls",
        "supervisor_calls",
        "conservative_supervisor_calls",
        "supervisor_input_tokens",
        "supervisor_output_tokens",
        "attempts",
        "commits",
        "checkpoints",
        "reconciliations",
        "runtime_receipts",
        "operator_commands",
        "input_tokens",
        "output_tokens",
        "duration_ms",
        "known_cost",
        "unknown_cost_operations",
        "abandoned_attempts",
        "rejected_attempts",
        "stale_attempts",
        "incomparable_attempts",
        "invalid_attempts",
        "hard_constraint_failed_attempts",
    )
    return {name: getattr(counted, name) for name in names}


def _pairs(value: object) -> list[tuple[str, str]]:
    """Return string pairs stored as lists or rehydrated tuples."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    pairs: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            continue
        if len(item) >= 2 and isinstance(item[0], str) and isinstance(item[1], str):
            pairs.append((item[0], item[1]))
    return pairs


def _request_id(event: Event) -> str | None:
    """Return a call's request id when the payload records one."""
    return _string(event.payload, "request_id")


def _string(payload: Mapping[str, object], name: str) -> str | None:
    """Return one non-empty string field."""
    value = payload.get(name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _string_value(value: object) -> str | None:
    """Return a non-empty string value."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_int(value: object) -> int | None:
    """Return an integer field, ignoring booleans and other types."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _usage_int(payload: Mapping[str, object], name: str) -> int | None:
    """Return one integer from a journaled usage object."""
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    return _optional_int(usage.get(name))


def _artifact_digest(value: object) -> str | None:
    """Return the receipt artifact digest when the journal stored one."""
    if not isinstance(value, Mapping):
        return None
    digest = value.get("digest")
    if isinstance(digest, str) and len(digest) == 64:
        return digest
    return None


def _diagnosis(payload: Mapping[str, object]) -> str | None:
    """Return supervisor guidance diagnosis text."""
    guidance = payload.get("guidance")
    if not isinstance(guidance, Mapping):
        return None
    return _string(guidance, "diagnosis")


def _snippet(value: str | None) -> str | None:
    """Bound a journal string for the review screen."""
    if value is None:
        return None
    if len(value) <= _TEXT_LIMIT:
        return value
    return value[: _TEXT_LIMIT - 3] + "..."


def _short_arguments(value: object) -> str:
    """Render action arguments on one short line."""
    if not isinstance(value, Mapping) or not value:
        return ""
    parts: list[str] = []
    for key, item in value.items():
        rendered = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
        parts.append(f"{key}={_bound(rendered, 80)}")
    return _bound(" ".join(parts), _TEXT_LIMIT)


def _bound(value: str, limit: int) -> str:
    """Ellipsize one string to a fixed limit."""
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _sequence_key(value: object) -> int:
    """Sort missing sequences after recorded ones."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _age_seconds(opened: Event, closer: Event | None) -> float | None:
    """Return seconds from a wait to the event that cleared it."""
    if closer is None:
        return None
    start = _parse_time(opened.recorded_at)
    end = _parse_time(closer.recorded_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _parse_time(value: str) -> datetime | None:
    """Parse an event timestamp. Unparseable stamps stay unlabeled."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _last(events: tuple[Event, ...], kind: str) -> Event | None:
    """Return the last event of one kind."""
    found: Event | None = None
    for event in events:
        if event.kind == kind:
            found = event
    return found


def _last_wait(events: tuple[Event, ...]) -> Event | None:
    """Return the last durable wait event."""
    found: Event | None = None
    for event in events:
        if event.kind in {
            "agent_waited",
            "question_recorded",
            "provider_recovery_waiting",
        }:
            found = event
    return found


def _or_none(value: object) -> str:
    """Render an absent fact as the word none."""
    if value is None or value == "":
        return "none"
    return str(value)


def _format_stop(value: object) -> str:
    """Render the stop fact on one line."""
    if not isinstance(value, Mapping):
        return "why_stopped: none"
    reason = value.get("reason")
    detail = "" if not isinstance(reason, str) or not reason else f" reason={reason}"
    return (
        f"why_stopped: {value.get('stop_class')} "
        f"event={_or_none(value.get('event_kind'))} "
        f"sequence={_or_none(value.get('sequence'))}{detail}"
    )


def _format_settings(value: object) -> str:
    """Render the non-secret settings block."""
    if not isinstance(value, Mapping):
        return "settings: none"
    lines = ["settings:"]
    for key in (
        "objective_excerpt",
        "workspace",
        "target",
        "model",
        "base_url",
        "api_key_env",
        "shell",
        "max_actions",
        "max_model_calls",
        "max_evaluations",
        "max_steps",
        "token_budget",
        "max_output_tokens",
        "repair_budget",
        "evaluation_mode",
        "transport_recovery",
        "package_version",
        "session_id",
    ):
        lines.append(f"  {key}: {value.get(key)}")
    return "\n".join(lines)


def _format_gate(value: object) -> str:
    """Render the optional external gate cross-check."""
    if not isinstance(value, Mapping):
        return "gate_log: not supplied"
    lines = [
        "gate_log: external "
        f"passed={value.get('passed')} failed={value.get('failed')}"
    ]
    failures = value.get("failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, Mapping):
                lines.append(f"  FAIL {failure.get('check')}: {failure.get('detail')}")
    return "\n".join(lines)


def _format_counters(value: object) -> str:
    """Render the evidence counters used by the compare table."""
    if not isinstance(value, Mapping):
        return "counters: none"
    keys = (
        "actions",
        "evaluations",
        "model_calls",
        "supervisor_calls",
        "input_tokens",
        "output_tokens",
        "runtime_receipts",
        "attempts",
        "commits",
    )
    body = " ".join(f"{key}={value.get(key)}" for key in keys)
    return f"counters: {body}"


def _format_calls(value: object) -> list[str]:
    """Render one line per model or supervisor call."""
    if not isinstance(value, list) or not value:
        return ["  none"]
    lines: list[str] = []
    for call in value:
        if isinstance(call, Mapping):
            lines.append(_format_call(call))
    return lines or ["  none"]


def _format_call(call: Mapping[str, object]) -> str:
    """Render one call, including its omission reasons."""
    manifest = call.get("context_manifest")
    if isinstance(manifest, Mapping):
        reasons = manifest.get("omission_reasons")
        context = (
            f"manifest included={manifest.get('included_count')} "
            f"omitted={manifest.get('omitted_count')} reasons={reasons}"
        )
    elif call.get("role") == "supervisor":
        context = "context_manifest: none"
    else:
        context = "manifest: none"
    return (
        f"  {call.get('sequence')} {call.get('role')} {call.get('outcome')} "
        f"kind={_or_none(call.get('result_kind'))} "
        f"tokens={_or_none(call.get('input_tokens'))}/"
        f"{_or_none(call.get('output_tokens'))} "
        f"reasoning={_or_none(call.get('reasoning_tokens'))} "
        f"failure={_or_none(call.get('failure_class'))} "
        f"diagnosis={_or_none(call.get('diagnosis'))} {context}"
    )


def _format_actions(value: object) -> list[str]:
    """Render one line per action."""
    if not isinstance(value, list) or not value:
        return ["  none"]
    lines: list[str] = []
    for action in value:
        if not isinstance(action, Mapping):
            continue
        lines.append(
            f"  {action.get('sequence')} {action.get('action_name')} "
            f"args={action.get('arguments')} "
            f"rationale={action.get('rationale')} "
            f"receipt={_or_none(action.get('receipt_status'))} "
            f"error={_or_none(action.get('error'))} "
            f"digest={_or_none(action.get('artifact_digest'))}"
        )
    return lines or ["  none"]


def _format_marks(value: object) -> list[str]:
    """Render one line per tooling mark."""
    if not isinstance(value, list) or not value:
        return ["  none"]
    lines: list[str] = []
    for mark in value:
        if isinstance(mark, Mapping):
            lines.append(
                f"  {mark.get('sequence')} {mark.get('kind')} {mark.get('fields')}"
            )
    return lines or ["  none"]


def _format_waits(value: object) -> list[str]:
    """Render one line per durable wait."""
    if not isinstance(value, list) or not value:
        return ["  none"]
    lines: list[str] = []
    for wait in value:
        if not isinstance(wait, Mapping):
            continue
        state = "open" if wait.get("open") else f"age={wait.get('age_seconds')}"
        lines.append(
            f"  {wait.get('sequence')} {wait.get('kind')} since={wait.get('since')} "
            f"{state} deadline={wait.get('deadline')} "
            f"reason={wait.get('reason')}"
        )
    return lines or ["  none"]


def _format_compare_row(row: Mapping[str, object]) -> str:
    """Render one compare table row."""
    directory = row.get("directory")
    label = Path(str(directory)).name if isinstance(directory, str) else "run"
    tokens = f"{row.get('input_tokens')}/{row.get('output_tokens')}"
    plan = "yes" if row.get("plan_proposed") else "no"
    evaluation = "yes" if row.get("evaluation_requested") else "no"
    return (
        f"{label} {row.get('calls')} {row.get('actions')} {tokens} "
        f"{row.get('suppressions')} {plan} {evaluation} "
        f"{row.get('supervision_fires')} {row.get('unknown_outcomes')} "
        f"{row.get('stop_class')} {row.get('stop_reason')}"
    )
