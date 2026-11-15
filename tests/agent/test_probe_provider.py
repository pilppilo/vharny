"""The probe-provider verb makes exactly one live readiness call and reports it."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

import vharness.agent.cli as cli_module
from vharness.agent.application import PROVIDER_HEADERS_ENV
from vharness.agent.cli import main


def _probe(*extra: str) -> tuple[int, str, str]:
    output = StringIO()
    error = StringIO()
    argv = [
        "probe-provider",
        "--base-url",
        "http://localhost:1/v1",
        "--model",
        "probe-model",
        *extra,
    ]
    code = main(argv, output=output, error=error)
    return code, output.getvalue(), error.getvalue()


class _Generation:
    def __init__(self, **kwargs: object) -> None:
        self.text = kwargs.get("text", "")
        self.error = kwargs.get("error")
        self.finish_reason = kwargs.get("finish_reason", "stop")
        self.failure_class = kwargs.get("failure_class")
        self.latency = kwargs.get("latency", 0.25)
        self.prompt_tokens = kwargs.get("prompt_tokens", 7)
        self.completion_tokens = kwargs.get("completion_tokens", 5)
        self.reasoning_tokens = kwargs.get("reasoning_tokens")

    @property
    def ok(self) -> bool:
        return self.error is None


class _RecordingGenerator:
    provider_id = "openai-compatible"

    def __init__(self, _base_url: str, _model: str, _api_key: str, **kwargs: object):
        type(self).constructor_kwargs = kwargs
        self.generation = type(self).generation
        self.calls = 0
        type(self).instances.append(self)

    def generate(self, system: str, prompt: str) -> _Generation:
        del system, prompt
        self.calls += 1
        return self.generation

    def close(self) -> None:
        pass


@pytest.fixture
def probe_generator(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingGenerator]:
    class _Generator(_RecordingGenerator):
        generation = _Generation(text="unused")
        constructor_kwargs: dict[str, object] = {}
        instances: list["_Generator"] = []

    monkeypatch.setattr(cli_module, "OpenAICompatible", _Generator)
    return _Generator


@pytest.fixture
def key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROBE_KEY", "secret-value")
    monkeypatch.delenv(PROVIDER_HEADERS_ENV, raising=False)


def test_ready_probe_exits_zero_and_reports_usage(
    monkeypatch: pytest.MonkeyPatch,
    key_env: None,
    probe_generator: type[_RecordingGenerator],
) -> None:
    probe_generator.generation = _Generation(text='{"probe": "ok"}', reasoning_tokens=3)
    monkeypatch.setenv("TEST_PROBE_KEY", "secret-value")
    code, out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")
    report = json.loads(out.strip())

    assert code == 0
    assert report["ready"] is True
    assert report["provider_call_made"] is True
    assert report["api_key_available"] is True
    assert report["extra_headers_present"] is False
    assert report["finish_reason"] == "stop"
    assert report["reasoning_tokens"] == 3
    assert probe_generator.constructor_kwargs["max_retries"] == 0
    assert probe_generator.constructor_kwargs["length_retries"] == 0


def test_missing_key_reports_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
    probe_generator: type[_RecordingGenerator],
) -> None:
    monkeypatch.delenv("TEST_PROBE_KEY", raising=False)
    code, out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")
    report = json.loads(out.strip())

    assert code == 1
    assert probe_generator.instances == []
    assert report["provider_call_made"] is False
    assert report["api_key_available"] is False
    assert "not set" in report["error"]


def test_auth_failure_is_classified_and_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch,
    key_env: None,
    probe_generator: type[_RecordingGenerator],
) -> None:
    probe_generator.generation = _Generation(
        error="AuthenticationError: Error code: 401 - secret-value leaked",
        failure_class="authentication",
    )
    code, out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")
    report = json.loads(out.strip())

    assert code == 1
    assert report["ready"] is False
    assert report["provider_call_made"] is True
    assert report["failure_class"] == "authentication"
    assert "secret-value" not in out
    assert "[REDACTED]" in report["error"]


def test_empty_output_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    key_env: None,
    probe_generator: type[_RecordingGenerator],
) -> None:
    probe_generator.generation = _Generation(text="", finish_reason="length")
    code, out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")
    report = json.loads(out.strip())

    assert code == 1
    assert report["ready"] is False
    assert report["failure_class"] is None


def test_extra_headers_presence_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    key_env: None,
    probe_generator: type[_RecordingGenerator],
) -> None:
    monkeypatch.setenv(
        PROVIDER_HEADERS_ENV, json.dumps({"x-opencode-session": "probe-run"})
    )
    probe_generator.generation = _Generation(text="ok")
    code, out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")
    report = json.loads(out.strip())

    assert code == 0
    assert report["extra_headers_present"] is True
    assert probe_generator.constructor_kwargs["extra_headers"] == {
        "x-opencode-session": "probe-run"
    }


def test_exactly_one_call_per_probe(
    key_env: None,
    probe_generator: type[_RecordingGenerator],
) -> None:
    probe_generator.generation = _Generation(text="ok")
    code, _out, _err = _probe("--api-key-env", "TEST_PROBE_KEY")

    assert code == 0
    assert len(probe_generator.instances) == 1
    assert probe_generator.instances[0].calls == 1
