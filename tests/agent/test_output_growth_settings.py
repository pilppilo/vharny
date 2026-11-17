"""The answer-limit growth setting (COD-88): validation, manifest and identity."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vharness.agent.application import run_identity
from vharness.agent.cli import _build_parser
from vharness.agent.errors import ContractError
from vharness.agent.run_settings import RunSettings


def _settings(**overrides) -> RunSettings:
    settings = RunSettings(
        objective="o",
        workspace="/tmp/workspace",
        base_url="http://localhost:1/v1",
        model="m",
        api_key_env="K",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=4,
        max_steps=20,
        token_budget=65536,
        max_output_tokens=32768,
        repair_budget=0,
    )
    return replace(settings, **overrides) if overrides else settings


class _Fixed:
    def identity(self) -> str:
        return "fixed"


def test_growth_is_recorded_only_when_set():
    plain = _settings()
    assert "initial_output_tokens" not in plain.to_payload()
    assert RunSettings.from_payload(plain.to_payload()).initial_output_tokens is None
    grown = _settings(initial_output_tokens=512)
    assert grown.to_payload()["initial_output_tokens"] == 512
    assert RunSettings.from_payload(grown.to_payload()) == grown


@pytest.mark.parametrize("value", [0, -1, 40000, True])
def test_growth_start_must_be_positive_and_within_the_maximum(value):
    with pytest.raises(ContractError, match="initial_output_tokens"):
        _settings(initial_output_tokens=value)


def test_a_bad_persisted_growth_start_is_refused():
    payload = {**_settings().to_payload(), "initial_output_tokens": "512"}
    with pytest.raises(ContractError, match="initial_output_tokens"):
        RunSettings.from_payload(payload)


def test_growth_changes_the_run_identity_only_when_set():
    plain = run_identity(_settings(), _Fixed(), _Fixed())
    assert plain == run_identity(_settings(), _Fixed(), _Fixed())
    grown = run_identity(_settings(initial_output_tokens=512), _Fixed(), _Fixed())
    assert grown.configuration_digest != plain.configuration_digest


def test_the_run_command_accepts_the_growth_start():
    parser = _build_parser()  # pylint: disable=protected-access
    arguments = parser.parse_args(
        ["run", "--objective", "o", "--initial-output-tokens", "512"]
    )
    assert arguments.initial_output_tokens == 512
    assert parser.parse_args(["run", "--objective", "o"]).initial_output_tokens is None
