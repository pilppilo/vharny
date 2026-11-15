"""Checks for the Markdown attempt note built from a runview projection."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from vharness.agent.errors import ContractError
from tools.runview import main as runview_main
from vharness.agent.runview_note import build_note, main

DIGEST = "a" * 64
OTHER = "b" * 64


def _view() -> dict[str, object]:
    """Return one projection shaped like ``runview --json``."""
    return {
        "directory": "/runs/run-1",
        "settings": {
            "objective_excerpt": "inspect the fixture",
            "workspace": "/work",
            "target": "127.0.0.1",
            "model": "fixture-model",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "LONGCAT_API_KEY",
            "shell": "/bin/sh",
            "max_actions": 16,
            "max_model_calls": 20,
            "max_evaluations": 1,
            "max_steps": 40,
            "token_budget": 16384,
            "max_output_tokens": 256,
            "repair_budget": 0,
            "evaluation_mode": "operator",
            "transport_recovery": None,
            "package_version": "0.2.0",
            "session_id": "session-1",
        },
        "why_stopped": {
            "stop_class": "open",
            "event_kind": "model_malformed",
            "sequence": 9,
            "reason": "model transport failure",
        },
        "actions": [
            {
                "sequence": 2,
                "proposal_id": "p1",
                "request_id": "r1",
                "action_name": "inspect",
                "arguments": "SECRET TOOL OUTPUT",
                "rationale": "because | the list is stale\nsecond line",
                "receipt_status": "succeeded",
                "receipt_sequence": 4,
                "artifact_digest": DIGEST,
                "error": "raw error body",
            },
            {
                "sequence": 3,
                "proposal_id": "p2",
                "request_id": "r1",
                "action_name": "inspect",
                "arguments": "SECOND SECRET",
                "rationale": "second reason",
                "receipt_status": "failed",
                "receipt_sequence": 5,
                "artifact_digest": OTHER,
                "error": "raw error body",
            },
        ],
    }


def test_note_records_settings_stop_rationales_and_hashes() -> None:
    """The four recorded categories reach the note."""
    note = build_note(_view())
    assert "# Attempt note" in note
    assert "inspect the fixture" in note
    assert "fixture-model" in note
    assert "LONGCAT_API_KEY" in note
    assert "16384" in note
    assert "- Class: open" in note
    assert "model_malformed (sequence 9)" in note
    assert "model transport failure" in note
    assert "because \\| the list is stale second line" in note
    assert "second reason" in note
    assert f"`{DIGEST}`" in note
    assert f"`{OTHER}`" in note


def test_note_excludes_arguments_errors_and_bodies() -> None:
    """No raw tool material reaches the note."""
    note = build_note(_view())
    assert "SECRET TOOL OUTPUT" not in note
    assert "SECOND SECRET" not in note
    assert "raw error body" not in note
    assert "ss -lnt" not in note


def test_note_lists_each_hash_once() -> None:
    """Repeated digests do not repeat the hash list entry."""
    view = _view()
    actions = view["actions"]
    assert isinstance(actions, list)
    actions.append({**actions[0], "sequence": 6})
    note = build_note(view)
    assert note.count(f"`{DIGEST}`") == 1


def test_note_requires_the_settings_block() -> None:
    """A projection that is not a runview show output is refused."""
    view = _view()
    del view["settings"]
    with pytest.raises(ContractError):
        build_note(view)


def test_runview_note_subcommand_writes_the_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``runview note`` renders the same note as the sibling command."""
    source = tmp_path / "view.json"
    source.write_text(json.dumps(_view()), encoding="utf-8")
    output = tmp_path / "note.md"
    assert runview_main(["note", str(source), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == build_note(_view())
    assert "wrote" in capsys.readouterr().err


def test_note_command_writes_the_markdown(tmp_path: Path, capsys) -> None:
    """The command line renders the same note as the library."""
    source = tmp_path / "view.json"
    source.write_text(json.dumps(_view()), encoding="utf-8")
    output = tmp_path / "note.md"
    assert main([str(source), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == build_note(_view())
    assert "wrote" in capsys.readouterr().err


def test_note_command_reads_stdin(monkeypatch, capsys) -> None:
    """The projection may arrive on standard input."""
    stream = io.TextIOWrapper(io.BytesIO(json.dumps(_view()).encode("utf-8")))
    monkeypatch.setattr(sys, "stdin", stream)
    assert main(["-"]) == 0
    assert capsys.readouterr().out == build_note(_view())
