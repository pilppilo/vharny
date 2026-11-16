"""Transport recovery: typed failure classes, coordinator retries, durable wait.

Covers the ADR-0013 recovery boundary: the provider adapter classifies
sanitized failure classes, the coordinator retries only retryable transport
failures within the journaled recovery window, exhaustion enters the durable
provider-recovery wait that burns no calls, and the operator surface exposes
and ends the wait through the existing resume, steer, and stop commands.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from vharness.agent import ContractError, SessionStatus
from vharness.agent.application import Application, RunSettings
from vharness.agent.control import ACCEPTED, command_frame
from vharness.agent.kernel import (
    CompatibleModel,
    ModelCallError,
    normalize_failure_class,
)
from vharness.agent.models import PROVIDER_RECOVERY_WAIT, Pause, Resume, Steer
from vharness.agent.run_settings import TransportRecoverySettings
from vharness.core import Generation, ModelFailureClass


class _ScriptedTransport:
    """Fake text generator whose calls follow a scripted failure schedule.

    Script entries are ``Exception`` instances to raise or ``Generation``
    records to return. An exhausted script raises, which the kernel records as
    an unclassified transport failure that the recovery policy never retries.
    """

    def __init__(self, script: tuple[Generation | Exception, ...]) -> None:
        self._script = list(script)
        self.max_retries = 0
        self.length_retries = 0
        self.calls = 0

    def extend(self, *entries: Generation | Exception) -> None:
        """Append further scripted entries for a later operator-driven retry."""
        self._script.extend(entries)

    def generate(self, system: str, prompt: str) -> Generation:
        del system, prompt
        self.calls += 1
        if self._script:
            entry = self._script.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry
        raise RuntimeError("scripted transport is exhausted")


def _transport_failure(
    failure_class: str = ModelFailureClass.TRANSIENT_CONNECTION.value,
) -> Generation:
    """Return one failed generation carrying a sanitized failure class."""
    return Generation(
        text="",
        model="scripted-transport",
        error="transport failure",
        failure_class=failure_class,
    )


def _lineage_success() -> Generation:
    """Return one successful generation yielding a dispatch-free result."""
    return Generation(
        text=json.dumps({"kind": "LineageQuery", "node_id": None}),
        model="scripted-transport",
        finish_reason="stop",
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    return workspace


def _settings(workspace: Path, **overrides) -> RunSettings:
    settings = RunSettings(
        objective="fix the fixture",
        workspace=str(workspace),
        base_url="http://localhost:1/v1",
        model="test-model",
        api_key_env="TEST_KEY",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=4,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
    )
    return replace(settings, **overrides) if overrides else settings


def _fast_recovery() -> TransportRecoverySettings:
    """Return the recovery policy with test-scale backoff delays."""
    return TransportRecoverySettings(max_retries=2, retry_delays_seconds=(0.001, 0.001))


def _application(
    tmp_path: Path,
    session_id: str,
    transport: _ScriptedTransport,
    **overrides,
) -> Application:
    """Create one application over the scripted transport."""
    settings = _settings(
        _workspace(tmp_path),
        transport_recovery=_fast_recovery(),
        **overrides,
    )
    model = CompatibleModel(transport, model_id="scripted-transport", repair_budget=0)
    return Application.create(
        settings,
        tmp_path / "run",
        session_id=session_id,
        model=model,
        poll_seconds=0,
    )


def _kinds(app: Application) -> list[str]:
    return [event.kind for event in app.session.events(limit=1000)]


def _events_of(app: Application, kind: str) -> list:
    return [event for event in app.session.events(limit=1000) if event.kind == kind]


def _drive_until(
    app: Application,
    predicate: Callable[[Application], bool],
    *,
    timeout_seconds: float = 10.0,
) -> None:
    """Tick the coordinator loop until the predicate holds or the time bound trips.

    Model calls run on a worker thread, so a tick can return before the call
    settles. A tick-count bound raced that worker under load (about one run in
    five); a wall-clock bound with a short pause between ticks does not.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate(app):
            return
        app.tick()
        time.sleep(0.001)
    if predicate(app):
        return
    raise AssertionError("coordinator loop did not reach the expected state")


def _openai_http():
    """The HTTP library the installed openai SDK builds its errors from.

    openai 1.x and 2.x use ``httpx``; 3.x uses ``httpx2``. Taking the module
    from the SDK keeps these tests running on every supported major version.
    """
    from openai import _exceptions  # pylint: disable=import-outside-toplevel

    http = getattr(_exceptions, "httpx2", None) or getattr(_exceptions, "httpx", None)
    assert http is not None, "openai no longer exposes its HTTP library"
    return http


def test_adapter_classifies_provider_exceptions():
    httpx = _openai_http()
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    from vharness.generators.openai_compat import _failure_class

    request = httpx.Request("POST", "http://test/v1")
    assert _failure_class(APITimeoutError(request)) == "transient-timeout"
    assert _failure_class(APIConnectionError(request=request)) == "transient-connection"
    for status, expected in (
        (429, "rate-limit"),
        (500, "transient-server"),
        (401, "authentication"),
        (400, "invalid-request"),
    ):
        response = httpx.Response(status, request=request)
        error = APIStatusError("rejected", response=response, body=None)
        assert _failure_class(error) == expected
    assert _failure_class(ValueError("not a provider error")) == "unknown"


def test_adapter_error_generation_carries_the_failure_class(monkeypatch):
    httpx = _openai_http()
    from openai import APIConnectionError

    from vharness.generators.openai_compat import OpenAICompatible

    generator = OpenAICompatible("http://127.0.0.1:1/v1", "test-model", "key")
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")

    def raise_connection(*args, **kwargs):
        raise APIConnectionError(request=request)

    monkeypatch.setattr(generator._client.chat.completions, "create", raise_connection)
    try:
        generation = generator.generate("system", "prompt")
        assert generation.error is not None
        assert generation.failure_class == "transient-connection"
    finally:
        generator.close()


def test_failure_class_normalization_rejects_unknown_values():
    assert normalize_failure_class("transient-connection") == "transient-connection"
    assert normalize_failure_class(ModelFailureClass.RATE_LIMIT) == "rate-limit"
    assert normalize_failure_class("made-up") is None
    assert normalize_failure_class(None) is None


def test_transport_recovery_settings_round_trip_and_defaults(tmp_path):
    settings = _settings(
        _workspace(tmp_path), transport_recovery=TransportRecoverySettings()
    )
    restored = RunSettings.from_payload(settings.to_payload())
    assert restored == settings
    assert restored.max_recovery_seconds == pytest.approx(120.0 * 3 + 10.0)

    payload = settings.to_payload()
    del payload["transport_recovery"]
    defaults = RunSettings.from_payload(payload)
    assert defaults.transport_recovery == TransportRecoverySettings()
    assert defaults.transport_recovery.max_retries == 2
    assert defaults.transport_recovery.retry_delays_seconds == (2.0, 8.0)


def test_transport_recovery_settings_reject_mismatched_delays():
    with pytest.raises(ContractError):
        TransportRecoverySettings(max_retries=2, retry_delays_seconds=(1.0,))


def test_transient_failure_is_retried_and_then_settles(tmp_path):
    transport = _ScriptedTransport(
        (_transport_failure(), _transport_failure(), _lineage_success())
    )
    app = _application(tmp_path, "session-tr1", transport)
    try:
        _drive_until(app, lambda a: len(_events_of(a, "model_decided")) == 1)
        failures = _events_of(app, "model_malformed")
        assert [event.payload.get("failure_class") for event in failures] == [
            "transient-connection",
            "transient-connection",
        ]
        # A retry may still bill the provider even though the response was
        # lost: token and cost dimensions stay unknown, never zero.
        for event in failures:
            usage = event.payload.get("usage")
            assert usage is not None
            assert usage["input_tokens"] is None
            assert usage["output_tokens"] is None
            assert usage["cost"] is None
        assert transport.calls == 3
        assert "provider_recovery_waiting" not in _kinds(app)
    finally:
        app.close()


def test_retry_exhaustion_enters_the_durable_waiting_state(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(3)))
    app = _application(tmp_path, "session-tr2", transport)
    try:
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        assert app.view().wait_reason == PROVIDER_RECOVERY_WAIT
        assert transport.calls == 3
        waiting = _events_of(app, "provider_recovery_waiting")
        assert len(waiting) == 1
        payload = waiting[0].payload
        assert payload["failure_class"] == "transient-connection"
        assert payload["consumed_calls"] == 3
        assert payload["operator_action"] is False
        assert payload["detail"] == (
            "transport recovery exhausted after 3 failed calls"
        )
        resting = transport.calls
        for _ in range(3):
            app.tick()
        assert transport.calls == resting  # waiting burns no calls
    finally:
        app.close()


def test_resume_after_recovery_wait_retries_with_remaining_budget(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(3)))
    app = _application(tmp_path, "session-tr3", transport)
    try:
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        transport.extend(_lineage_success())
        reply = app.submit_frame(
            command_frame(Resume("resume-1", app.session.session_id))
        )
        assert reply.status == ACCEPTED
        _drive_until(app, lambda a: len(_events_of(a, "model_decided")) == 1)
        assert transport.calls == 4
        assert "provider_recovery_waiting" in _kinds(app)
    finally:
        app.close()


def test_authentication_failure_waits_immediately_without_retry(tmp_path):
    transport = _ScriptedTransport(
        (_transport_failure(ModelFailureClass.AUTHENTICATION.value),)
    )
    app = _application(tmp_path, "session-tr4", transport)
    try:
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        assert transport.calls == 1
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["failure_class"] == "authentication"
        assert payload["operator_action"] is True
        assert payload["detail"] == "operator action required: authentication failure"
    finally:
        app.close()


def test_nonretryable_failure_propagates_without_recovery(tmp_path):
    transport = _ScriptedTransport(
        (_transport_failure(ModelFailureClass.INVALID_REQUEST.value),)
    )
    app = _application(tmp_path, "session-tr5", transport)
    try:
        with pytest.raises(ModelCallError) as raised:
            app.tick()
        assert raised.value.failure_class == "invalid-request"
        assert _events_of(app, "model_malformed")
        assert "provider_recovery_waiting" not in _kinds(app)
        assert transport.calls == 1
    finally:
        app.close()


def test_malformed_output_is_not_retried_as_transport(tmp_path):
    """ADR-0015 rung 3: exhausted malformed output waits; it is never retried."""
    transport = _ScriptedTransport(
        (
            Generation(
                text="{not json", model="scripted-transport", finish_reason="stop"
            ),
        )
    )
    app = _application(tmp_path, "session-tr6", transport)
    try:
        assert app.tick() == "decided"  # the decision unit commits its failure
        assert app.view().status is SessionStatus.WAITING
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["failure_class"] == "malformed-output"
        assert payload["operator_action"] is False
        assert transport.calls == 1  # the decode ladder owns malformed output
    finally:
        app.close()


def test_pause_during_backoff_holds_the_retry_until_resume(tmp_path):
    transport = _ScriptedTransport((_transport_failure(), _lineage_success()))
    app = _application(tmp_path, "session-tr7", transport)
    try:
        assert app.tick() == "decided"
        reply = app.submit_frame(
            command_frame(Pause("pause-1", app.session.session_id))
        )
        assert reply.status == ACCEPTED
        assert app.tick() == "idle"
        assert app.view().status is SessionStatus.PAUSED
        held = transport.calls
        app.tick()
        assert transport.calls == held  # a paused backoff dispatches nothing
        reply = app.submit_frame(
            command_frame(Resume("resume-1", app.session.session_id))
        )
        assert reply.status == ACCEPTED
        _drive_until(app, lambda a: len(_events_of(a, "model_decided")) == 1)
        assert transport.calls == held + 1
    finally:
        app.close()


def test_steer_during_backoff_starts_a_fresh_recovery_budget(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(4)))
    app = _application(tmp_path, "session-tr8", transport)
    try:
        assert app.tick() == "decided"  # objective v1 failure; retry scheduled
        reply = app.submit_frame(
            command_frame(
                Steer(
                    "steer-1",
                    app.session.session_id,
                    "new objective",
                    "operator evidence",
                )
            )
        )
        assert reply.status == ACCEPTED
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        # Steering invalidated the old decision: the fresh objective counts its
        # own recovery window instead of inheriting the earlier failure.
        assert payload["consumed_calls"] == 3
        assert transport.calls == 4
        assert {
            event.objective_version for event in _events_of(app, "model_malformed")
        } == {1, 2}
    finally:
        app.close()


def test_restart_during_backoff_respects_the_journaled_window(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(3)))
    app = _application(tmp_path, "session-tr9", transport)
    try:
        app.tick()  # failure one settled durably; retry scheduled in memory
    finally:
        app.close()

    reopened = Application.open(
        tmp_path / "run",
        model=CompatibleModel(
            transport, model_id="scripted-transport", repair_budget=0
        ),
        poll_seconds=0,
    )
    try:
        _drive_until(reopened, lambda a: a.view().status is SessionStatus.WAITING)
        # The window is journal-derived: the reopened coordinator keeps one
        # retry instead of silently restarting a fresh recovery budget.
        assert transport.calls == 3
        payload = _events_of(reopened, "provider_recovery_waiting")[0].payload
        assert payload["consumed_calls"] == 3
    finally:
        reopened.close()


def test_budget_exhaustion_during_recovery_enters_the_waiting_state(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(2)))
    app = _application(tmp_path, "session-tr10", transport, max_model_calls=2)
    try:
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        payload = _events_of(app, "provider_recovery_waiting")[0].payload
        assert payload["detail"] == "resource budget exhausted"
        assert payload["operator_action"] is False
        assert payload["consumed_calls"] == 2
        assert transport.calls == 2
    finally:
        app.close()


def test_status_exposes_the_provider_recovery_wait(tmp_path):
    transport = _ScriptedTransport(tuple(_transport_failure() for _ in range(3)))
    app = _application(tmp_path, "session-tr11", transport)
    try:
        _drive_until(app, lambda a: a.view().status is SessionStatus.WAITING)
        assert app.view().wait_reason == PROVIDER_RECOVERY_WAIT
        view = app.session.operator_view()
        assert view.wait_reason == PROVIDER_RECOVERY_WAIT
        assert any("RECOVERY waiting" in line for line in view.action_receipt_stream)
    finally:
        app.close()
