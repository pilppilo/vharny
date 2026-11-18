"""Rate cards and per-measurement dollar cost (COD-86)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from vharness.agent import CompatibleModel
from vharness.agent.errors import ContractError
from vharness.agent.kernel import ModelAttempt
from vharness.agent.pricing import RateCard

# LongCat-2.5 limited-time list rates, USD per million tokens.
LONGCAT = RateCard(
    input_per_million=Decimal("0.30"),
    output_per_million=Decimal("1.20"),
    cached_input_per_million=Decimal("0.006"),
    label="longcat-2.5 limited-time",
)


def test_cost_matches_the_billing_formula() -> None:
    cost = LONGCAT.cost(1_000_000, 0, cached_input_tokens=800_000)
    assert cost == Decimal("0.0648")
    assert LONGCAT.cost(1_000_000, 1_000_000) == Decimal("1.50")


def test_unknown_tokens_make_cost_unknown_not_zero() -> None:
    assert LONGCAT.cost(None, 10) is None
    assert LONGCAT.cost(10, None) is None


def test_missing_cache_split_prices_everything_uncached() -> None:
    assert LONGCAT.cost(1_000_000, 0) == Decimal("0.30")
    no_cache_rate = RateCard(Decimal("0.30"), Decimal("1.20"))
    assert no_cache_rate.cost(1_000_000, 0, cached_input_tokens=800_000) == Decimal(
        "0.30"
    )


def test_worst_case_never_assumes_cache_hits() -> None:
    assert LONGCAT.worst_case(1_000_000, 0) == Decimal("0.30")


@pytest.mark.parametrize(
    "build",
    [
        lambda: RateCard(Decimal("-1"), Decimal("1")),
        lambda: RateCard(Decimal("1"), Decimal("NaN")),
        lambda: RateCard(Decimal("1"), Decimal("1"), Decimal("-0.1")),
    ],
)
def test_bad_rates_are_refused(build) -> None:
    with pytest.raises(ContractError):
        build()


def test_bad_token_counts_are_refused() -> None:
    with pytest.raises(ContractError):
        LONGCAT.cost(-1, 0)
    with pytest.raises(ContractError):
        LONGCAT.cost(10, 0, cached_input_tokens=11)


class _Generator:
    def generate(self, system: str, prompt: str):  # pragma: no cover - unused
        raise AssertionError("not called")


def _attempts(*pairs: tuple[int | None, int | None]) -> list[ModelAttempt]:
    return [
        ModelAttempt(
            output="", finish_reason="stop", prompt_tokens=p, completion_tokens=c
        )
        for p, c in pairs
    ]


def test_model_prices_usage_across_all_attempts() -> None:
    model = CompatibleModel(_Generator(), rate_card=LONGCAT)
    request = SimpleNamespace(request_id="req-1")
    usage = model._usage(request, _attempts((1000, 200), (3000, 400)))  # type: ignore[arg-type]
    assert usage.input_tokens == 4000
    assert usage.output_tokens == 600
    assert usage.cost == Decimal("0.00192")


def test_model_without_a_rate_card_leaves_cost_unknown() -> None:
    model = CompatibleModel(_Generator())
    request = SimpleNamespace(request_id="req-1")
    usage = model._usage(request, _attempts((1000, 200)))  # type: ignore[arg-type]
    assert usage.cost is None


def test_one_unreported_attempt_keeps_cost_unknown() -> None:
    model = CompatibleModel(_Generator(), rate_card=LONGCAT)
    request = SimpleNamespace(request_id="req-1")
    usage = model._usage(request, _attempts((1000, 200), (None, None)))  # type: ignore[arg-type]
    assert usage.cost is None
