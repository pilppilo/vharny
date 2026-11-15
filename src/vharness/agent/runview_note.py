"""Markdown attempt note built from one ``runview --json`` projection.

The note records the settings around a run, why it stopped, each action's
rationale, and the artifact hashes that were produced. Arguments, errors, and
artifact bodies are deliberately excluded so tool output never reaches it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .codec import parse_json_object
from .errors import ContractError
from .runview_cli import run_command

_UNKNOWN = "not recorded"
_SETTINGS = (
    ("objective_excerpt", "Objective"),
    ("workspace", "Workspace"),
    ("target", "Target"),
    ("model", "Model"),
    ("base_url", "Base URL"),
    ("api_key_env", "API key env"),
    ("shell", "Shell"),
    ("max_actions", "Max actions"),
    ("max_model_calls", "Max model calls"),
    ("max_evaluations", "Max evaluations"),
    ("max_steps", "Max steps"),
    ("token_budget", "Token budget"),
    ("max_output_tokens", "Max output tokens"),
    ("repair_budget", "Repair budget"),
    ("evaluation_mode", "Evaluation mode"),
    ("transport_recovery", "Transport recovery"),
    ("package_version", "Package version"),
    ("session_id", "Session id"),
)


def build_note(view: Mapping[str, object]) -> str:
    """Return the Markdown attempt note for one runview projection."""
    settings = _require_mapping(view, "settings")
    stopped = _require_mapping(view, "why_stopped")
    actions = _actions(view)
    sections: list[str] = ["# Attempt note", ""]
    sections.extend(_identity(view))
    sections.extend(_settings(settings))
    sections.extend(_stop(stopped))
    sections.extend(_action_table(actions))
    sections.extend(_hashes(actions))
    return "\n".join(sections) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the attempt note command line and return a process status."""
    return run_command(_parser(), argv, _execute)


def _execute(args: argparse.Namespace) -> None:
    """Run the parsed attempt note command."""
    raw = sys.stdin.buffer.read() if args.source == "-" else _read(args.source)
    note = build_note(parse_json_object(raw))
    if args.output is None:
        sys.stdout.write(note)
    else:
        Path(args.output).write_text(note, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    """Build the attempt note argument parser."""
    parser = argparse.ArgumentParser(
        prog="runview-note",
        description="Render an attempt note from a runview JSON projection.",
    )
    parser.add_argument("source", help="runview --json output, or - for stdin")
    parser.add_argument("--output", "-o")
    return parser


def _read(source: str) -> bytes:
    """Read the serialized projection from one file path."""
    try:
        return Path(source).read_bytes()
    except OSError as exc:
        raise ContractError(f"could not read projection {source}: {exc}") from exc


def _require_mapping(view: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Return one required projection block or fail."""
    value = view.get(name)
    if not isinstance(value, Mapping):
        raise ContractError(f"projection has no {name} block")
    return value


def _actions(view: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    """Return the recorded action rows from one projection."""
    value = view.get("actions")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError("projection has no actions list")
    return [item for item in value if isinstance(item, Mapping)]


def _identity(view: Mapping[str, object]) -> list[str]:
    """Return the run directory line when the projection recorded one."""
    directory = view.get("directory")
    if not isinstance(directory, str):
        return []
    return [f"Run: `{directory}`", ""]


def _settings(settings: Mapping[str, object]) -> list[str]:
    """Return the settings table as one bullet per recorded setting."""
    lines = ["## Settings", ""]
    for key, label in _SETTINGS:
        lines.append(f"- {label}: {_render(settings.get(key))}")
    return [*lines, ""]


def _stop(stopped: Mapping[str, object]) -> list[str]:
    """Return the stop class, terminal event, and safe reason."""
    event = _render(stopped.get("event_kind"))
    sequence = stopped.get("sequence")
    if isinstance(sequence, int):
        event = f"{event} (sequence {sequence})"
    lines = [
        "## Stop",
        "",
        f"- Class: {_render(stopped.get('stop_class'))}",
        f"- Event: {event}",
        f"- Reason: {_render(stopped.get('reason'))}",
        "",
    ]
    return lines


def _action_table(actions: Sequence[Mapping[str, object]]) -> list[str]:
    """Return one row per action: name, rationale, receipt, artifact hash."""
    lines = ["## Actions", ""]
    if not actions:
        return [*lines, "none recorded", ""]
    lines.append("| Sequence | Action | Rationale | Receipt | Artifact |")
    lines.append("| --- | --- | --- | --- | --- |")
    for action in actions:
        sequence = _cell(action.get("sequence"))
        name = _cell(action.get("action_name"))
        rationale = _cell(action.get("rationale"))
        receipt = _cell(action.get("receipt_status"))
        digest = _cell(action.get("artifact_digest"))
        lines.append(f"| {sequence} | {name} | {rationale} | {receipt} | {digest} |")
    return [*lines, ""]


def _hashes(actions: Sequence[Mapping[str, object]]) -> list[str]:
    """Return each distinct artifact hash once, in first-seen order."""
    lines = ["## Artifact hashes", ""]
    seen: set[str] = set()
    for action in actions:
        digest = action.get("artifact_digest")
        if not isinstance(digest, str) or digest in seen:
            continue
        seen.add(digest)
        lines.append(f"- `{digest}` - {_render(action.get('action_name'))}")
    if not seen:
        lines.append("- none recorded")
    return [*lines, ""]


def _cell(value: object) -> str:
    """Render one Markdown table cell without breaking the row."""
    return _render(value).replace("|", "\\|").replace("\n", " ")


def _render(value: object) -> str:
    """Render a projection value as bounded plain text."""
    if value is None:
        return _UNKNOWN
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)
