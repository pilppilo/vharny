"""Provider rate cards: turn billed tokens into a dollar cost (COD-86).

A rate card is operator-supplied configuration, never discovered. Provider
list prices can be promotional and can change, so a card names the rates the
operator is budgeting against and the provider's own billing record stays
authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import ContractError

_PER = Decimal(1_000_000)


def _rate(name: str, value: Decimal | None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractError(f"{name} must be a finite Decimal")
    if value < 0:
        raise ContractError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class RateCard:
    """Dollars per million tokens for one provider model.

    ``cached_input_per_million`` is the rate for prompt tokens the provider
    served from its prompt cache. When it is ``None`` every prompt token is
    billed at the input rate. Reasoning tokens are part of the answer and are
    billed at the output rate, never separately.
    """

    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    #: Free-text provenance, for example "longcat-2.5 limited-time rate".
    label: str = ""

    def __post_init__(self) -> None:
        _rate("input rate", self.input_per_million)
        _rate("output rate", self.output_per_million)
        _rate("cached input rate", self.cached_input_per_million)

    def cost(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int | None = None,
    ) -> Decimal | None:
        """Return the cost of one measurement, or ``None`` if it is unknown.

        Unknown input or output tokens make the cost unknown, never zero.
        ``cached_input_tokens`` of ``None`` means the provider did not report
        a cache split, so every prompt token is priced as uncached. That is an
        upper bound whenever the cached rate is lower.
        """
        if input_tokens is None or output_tokens is None:
            return None
        if input_tokens < 0 or output_tokens < 0:
            raise ContractError("token counts must not be negative")
        cached = cached_input_tokens or 0
        if cached < 0 or cached > input_tokens:
            raise ContractError("cached input tokens must lie within input tokens")
        cached_rate = (
            self.input_per_million
            if self.cached_input_per_million is None
            else self.cached_input_per_million
        )
        return (
            Decimal(input_tokens - cached) * self.input_per_million
            + Decimal(cached) * cached_rate
            + Decimal(output_tokens) * self.output_per_million
        ) / _PER

    def worst_case(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Return the cost with no cache hits, for reserving budget."""
        cost = self.cost(input_tokens, output_tokens)
        assert cost is not None
        return cost

    def to_payload(self) -> dict[str, str]:
        """Return the card as strings, with unset optional fields left out."""
        payload = {
            "input_per_million": str(self.input_per_million),
            "output_per_million": str(self.output_per_million),
        }
        if self.cached_input_per_million is not None:
            payload["cached_input_per_million"] = str(self.cached_input_per_million)
        if self.label:
            payload["label"] = self.label
        return payload

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> RateCard:
        """Decode a card, refusing unknown keys and malformed rates."""
        unknown = set(value) - _PAYLOAD_KEYS
        if unknown:
            raise ContractError(f"rate card has unknown keys: {sorted(unknown)}")
        for key in ("input_per_million", "output_per_million"):
            if key not in value:
                raise ContractError(f"rate card requires {key}")
        label = value.get("label", "")
        if not isinstance(label, str):
            raise ContractError("rate card label must be a string")
        cached = value.get("cached_input_per_million")
        return cls(
            input_per_million=_decimal(value["input_per_million"], "input rate"),
            output_per_million=_decimal(value["output_per_million"], "output rate"),
            cached_input_per_million=(
                None if cached is None else _decimal(cached, "cached input rate")
            ),
            label=label,
        )


_PAYLOAD_KEYS = frozenset(
    {"input_per_million", "output_per_million", "cached_input_per_million", "label"}
)


def _decimal(value: object, name: str) -> Decimal:
    """Parse a rate from a string or a number without going through a float."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ContractError(f"{name} must be a decimal string or number")
    try:
        return Decimal(value) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise ContractError(f"{name} is not a decimal number") from exc


def load_rate_card(path: str | Path) -> RateCard:
    """Read a rate card from a JSON file.

    Numbers are parsed as decimals, never floats, so 0.006 stays 0.006. The
    file is read once at launch and its contents are recorded in the run
    manifest, so a reopened run prices at the same rates.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read rate card {str(path)!r}: {exc}") from exc
    try:
        data = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ContractError(f"rate card is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("rate card must be a JSON object")
    return RateCard.from_payload(data)
