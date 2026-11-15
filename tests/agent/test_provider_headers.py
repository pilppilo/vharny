"""Operator-configured provider extra headers reach the model transports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vharness.agent.application as application_module
from vharness.agent.application import (
    PROVIDER_HEADERS_ENV,
    build_model,
    provider_extra_headers,
)
from vharness.agent.run_settings import RunSettings
from vharness.config import ConfigError


def _settings(workspace: Path) -> RunSettings:
    return RunSettings(
        objective="fix the fixture",
        workspace=str(workspace),
        base_url="http://localhost:1/v1",
        model="test-model",
        api_key_env="TEST_PROVIDER_HEADERS_KEY",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=4,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def test_unset_env_yields_no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROVIDER_HEADERS_ENV, raising=False)
    assert provider_extra_headers() == {}


def test_json_object_headers_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        PROVIDER_HEADERS_ENV, json.dumps({"x-opencode-session": "run-session"})
    )
    assert provider_extra_headers() == {"x-opencode-session": "run-session"}


@pytest.mark.parametrize(
    "raw", ["not json", '"text"', "[1]", '{"header": 3}', '{"header": null}']
)
def test_malformed_headers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(PROVIDER_HEADERS_ENV, raw)
    with pytest.raises(ConfigError):
        provider_extra_headers()


def test_generator_forwards_extra_headers_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openai")
    from vharness.generators import openai_compat

    captured: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai_compat, "OpenAI", _RecordingClient)
    openai_compat.OpenAICompatible(
        "http://localhost:1/v1", "m", "k", extra_headers={"A": "b"}
    )
    assert captured["default_headers"] == {"A": "b"}

    captured.clear()
    openai_compat.OpenAICompatible("http://localhost:1/v1", "m", "k")
    assert captured["default_headers"] is None


def test_build_model_wires_env_headers_into_the_generator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_HEADERS_KEY", "key-value")
    monkeypatch.setenv(
        PROVIDER_HEADERS_ENV, json.dumps({"x-opencode-session": "run-session"})
    )
    captured: dict[str, object] = {}
    real_generator = application_module.OpenAICompatible

    class _RecordingGenerator(real_generator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(application_module, "OpenAICompatible", _RecordingGenerator)
    settings = _settings(_workspace(tmp_path))
    _model, close = build_model(settings)
    try:
        assert captured["extra_headers"] == {"x-opencode-session": "run-session"}
    finally:
        if close is not None:
            close()


def test_build_model_omits_headers_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(PROVIDER_HEADERS_ENV, raising=False)
    monkeypatch.setenv("TEST_PROVIDER_HEADERS_KEY", "key-value")
    captured: dict[str, object] = {}
    real_generator = application_module.OpenAICompatible

    class _RecordingGenerator(real_generator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(application_module, "OpenAICompatible", _RecordingGenerator)
    settings = _settings(_workspace(tmp_path))
    _model, close = build_model(settings)
    try:
        assert captured["extra_headers"] is None
    finally:
        if close is not None:
            close()
