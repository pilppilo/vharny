"""Whole-run cost limit (COD-86): reservations, settlement and the rate-card file.

The coordinator tests mirror ``test_run_limits``: a scripted provider reports
fixed token usage, and running out of dollars ends the run with a
``budget_exhausted`` outcome for the ``cost`` resource.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from vharness.agent import SessionStatus
from vharness.agent.application import Application, RunSettings
from vharness.agent.errors import ContractError
from vharness.agent.kernel import CompatibleModel
from vharness.agent.models import BudgetLimits, ResourceReservation, UsageMeasurement
from vharness.agent.pricing import RateCard, load_rate_card
from vharness.agent.replay import replay
from vharness.agent.transitions import _costed
from vharness.agent.usage_ledger import UsageLedger
from vharness.core import Generation

# LongCat-2.5 limited-time list rates, USD per million tokens.
LONGCAT = RateCard(
    Decimal("0.30"), Decimal("1.20"), Decimal("0.006"), label="longcat-2.5"
)
# The scripted provider bills 8,000 prompt and 20 answer tokens per call.
BILLED = Decimal("0.002424")


class _Transport:
    def __init__(self) -> None:
        self.max_retries = 0
        self.length_retries = 0
        self.max_tokens = 256
        self.calls = 0

    def generate(self, system: str, prompt: str) -> Generation:
        del system, prompt
        self.calls += 1
        return Generation(
            text=json.dumps({"kind": "LineageQuery", "node_id": None}),
            model="scripted",
            finish_reason="stop",
            prompt_tokens=8000,
            completion_tokens=20,
        )


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
        rate_card=LONGCAT,
    )
    return replace(settings, **overrides) if overrides else settings


def _app(tmp_path: Path, transport: _Transport, **overrides) -> Application:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("value = 0\n", encoding="utf-8")
    settings = _settings(workspace, **overrides)
    return Application.create(
        settings,
        tmp_path / "run",
        session_id="session-cost",
        model=CompatibleModel(
            transport,
            model_id="scripted",
            repair_budget=0,
            rate_card=settings.rate_card,
        ),
        poll_seconds=0,
    )


def _events(app: Application, kind: str) -> list:
    return [event for event in app.session.events(limit=1000) if event.kind == kind]


def _drive_until_stopped(app: Application, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if app.view().status is SessionStatus.STOPPED:
            return
        app.tick()
        time.sleep(0.001)
    raise AssertionError("the run did not stop")


def _first_reservation_cost(tmp_path: Path) -> Decimal:
    probe = tmp_path / "probe"
    probe.mkdir()
    app = _app(probe, _Transport(), max_cost=Decimal("1000"))
    try:
        app.tick()
        reservation = _events(app, "model_intended")[0].payload["reservation"]
        return Decimal(reservation["cost"])
    finally:
        app.close()


def test_a_decision_reserves_its_worst_case_and_settles_to_billed_use(tmp_path):
    worst_case = _first_reservation_cost(tmp_path)
    assert worst_case > BILLED
    # Room for one worst case, not for the billed use plus a second one.
    transport = _Transport()
    app = _app(tmp_path, transport, max_cost=worst_case + Decimal("0.0001"))
    try:
        _drive_until_stopped(app)
        assert transport.calls == 1
        assert len(_events(app, "model_decided")) == 1
        payload = _events(app, "budget_exhausted")[0].payload
        assert payload == {"resource": "cost", "reason": "cost budget exhausted"}
        ledger = replay(app.session.events(limit=1000)).ledger
        assert ledger.known_cost == BILLED
        assert replay(app.session.events(limit=1000)).status is SessionStatus.STOPPED
    finally:
        app.close()


def test_a_limit_below_one_worst_case_stops_before_any_call(tmp_path):
    transport = _Transport()
    app = _app(tmp_path, transport, max_cost=Decimal("0.0001"))
    try:
        _drive_until_stopped(app)
        assert transport.calls == 0
        assert _events(app, "budget_exhausted")[0].payload["resource"] == "cost"
    finally:
        app.close()


def test_without_a_cost_limit_reservations_carry_no_cost(tmp_path):
    app = _app(tmp_path, _Transport())
    try:
        app.tick()
        reservation = _events(app, "model_intended")[0].payload["reservation"]
        assert reservation["cost"] is None
    finally:
        app.close()


def _state(limit: Decimal | None):
    return SimpleNamespace(task=SimpleNamespace(budgets=BudgetLimits(cost=limit)))


def test_actions_and_evaluations_cost_zero_under_a_cost_limit() -> None:
    limit = Decimal("1")
    reservation = ResourceReservation("r1", "op1", 1, actions=1, cost=Decimal(0))
    ledger = UsageLedger().reserve(reservation, BudgetLimits(cost=limit))
    # A settlement with no measurement would otherwise leave the cost unknown.
    costed = _costed(_state(limit), reservation, None)  # type: ignore[arg-type]
    assert costed is not None and costed.cost == Decimal(0)
    ledger = ledger.settle(reservation, costed)
    assert ledger.cost == Decimal(0)
    follow_up = ResourceReservation("r2", "op2", 1, actions=1, cost=Decimal(0))
    ledger.reserve(follow_up, BudgetLimits(cost=limit))  # no unknown exposure


def test_an_unpriced_model_call_keeps_its_reserved_worst_case() -> None:
    limit = Decimal("1")
    reservation = ResourceReservation(
        "r1", "op1", 1, model_calls=1, cost=Decimal("0.25")
    )
    unpriced = UsageMeasurement(measurement_id="m1", model_calls=1)
    costed = _costed(_state(limit), reservation, unpriced)  # type: ignore[arg-type]
    assert costed is not None and costed.cost == Decimal("0.25")
    assert _costed(_state(limit), reservation, None).cost == Decimal("0.25")  # type: ignore[arg-type, union-attr]


def test_a_priced_measurement_and_a_run_without_a_limit_are_untouched() -> None:
    reservation = ResourceReservation(
        "r1", "op1", 1, model_calls=1, cost=Decimal("0.25")
    )
    priced = UsageMeasurement(measurement_id="m1", model_calls=1, cost=Decimal("0.1"))
    assert _costed(_state(Decimal("1")), reservation, priced) is priced  # type: ignore[arg-type]
    unpriced = UsageMeasurement(measurement_id="m2", model_calls=1)
    assert _costed(_state(None), reservation, unpriced) is unpriced  # type: ignore[arg-type]
    assert _costed(_state(None), reservation, None) is None  # type: ignore[arg-type]


def test_settings_round_trip_and_stay_out_of_other_manifests(tmp_path) -> None:
    plain = _settings(tmp_path, rate_card=None)
    assert "max_cost" not in plain.to_payload()
    assert "rate_card" not in plain.to_payload()
    limited = _settings(tmp_path, max_cost=Decimal("12.50"))
    payload = limited.to_payload()
    assert payload["max_cost"] == "12.50"
    assert payload["rate_card"] == LONGCAT.to_payload()
    assert RunSettings.from_payload(payload) == limited


def test_a_cost_limit_requires_a_rate_card(tmp_path) -> None:
    with pytest.raises(ContractError):
        _settings(tmp_path, rate_card=None, max_cost=Decimal("1"))
    with pytest.raises(ContractError):
        _settings(tmp_path, max_cost=Decimal("0"))


def test_a_rate_card_file_loads_decimals_exactly(tmp_path) -> None:
    path = tmp_path / "card.json"
    path.write_text(
        '{"label": "longcat-2.5", "input_per_million": 0.30,'
        ' "cached_input_per_million": 0.006, "output_per_million": "1.20"}',
        encoding="utf-8",
    )
    assert load_rate_card(path) == LONGCAT


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "not json",
        '{"input_per_million": "1"}',
        '{"input_per_million": "1", "output_per_million": "2", "extra": 1}',
        '{"input_per_million": "x", "output_per_million": "2"}',
        '{"input_per_million": "-1", "output_per_million": "2"}',
    ],
)
def test_a_bad_rate_card_file_is_refused(tmp_path, text) -> None:
    path = tmp_path / "card.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ContractError):
        load_rate_card(path)
    with pytest.raises(ContractError):
        load_rate_card(tmp_path / "missing.json")


def test_an_unreported_operation_still_counts_against_other_limits() -> None:
    """A lost response under a cost limit counts as it would without one."""
    results = []
    for cost_limit in (None, Decimal("1")):
        limits = BudgetLimits(actions=1, model_calls=1, tokens=10_000, cost=cost_limit)
        state = _state(cost_limit)
        state.task.budgets = limits
        zero = Decimal(0) if cost_limit else None
        action = ResourceReservation("ra", "act", 1, actions=1, cost=zero)
        call = ResourceReservation(
            "rm",
            "mod",
            1,
            model_calls=1,
            tokens=5000,
            cost=Decimal("0.1") if cost_limit else None,
        )
        ledger = UsageLedger().reserve(action, limits).reserve(call, limits)
        ledger = ledger.settle(action, _costed(state, action, None))  # type: ignore[arg-type]
        ledger = ledger.settle(call, _costed(state, call, None))  # type: ignore[arg-type]
        results.append(
            (
                ledger._conservative_total("actions"),
                ledger._conservative_total("model_calls"),
                ledger.conservative_tokens,
            )
        )
    assert results[0] == results[1] == (1, 1, 5000)
