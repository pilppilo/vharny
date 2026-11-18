"""Run token limits reuse the durable reservation and settlement ledger."""

import pytest

from vharness.agent.errors import BudgetExhausted
from vharness.agent.models import BudgetLimits, ResourceReservation, UsageMeasurement
from vharness.agent.usage_ledger import UsageLedger


def test_token_reservation_is_replaced_by_billed_prompt_and_answer() -> None:
    limits = BudgetLimits(tokens=100)
    first = ResourceReservation("r1", "call1", 1, model_calls=1, tokens=80)
    second = ResourceReservation("r2", "call2", 1, model_calls=1, tokens=60)
    ledger = UsageLedger().reserve(first, limits)
    with pytest.raises(BudgetExhausted, match="token budget exhausted"):
        ledger.reserve(second, limits)

    ledger = ledger.settle(
        first,
        UsageMeasurement("m1", model_calls=1, input_tokens=12, output_tokens=28),
    )
    assert ledger.conservative_tokens == 40
    assert ledger.reserve(second, limits).conservative_tokens == 100


def test_token_reservation_survives_missing_provider_usage() -> None:
    limits = BudgetLimits(tokens=100)
    reservation = ResourceReservation("r1", "call1", 1, model_calls=1, tokens=80)
    ledger = UsageLedger().reserve(reservation, limits)
    assert ledger.settle(reservation, None).conservative_tokens == 80
    assert ledger.release(reservation).conservative_tokens == 0
