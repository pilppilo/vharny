"""Durable resource-accounting facts and budget admission."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from .errors import BudgetExhausted, TransitionError
from .models import (
    BudgetLimits,
    ResourceReservation,
    SUPERVISOR_REQUEST_PREFIX,
    UsageMeasurement,
)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One immutable provider measurement correlated to its operation."""

    operation_id: str
    measurement: UsageMeasurement


@dataclass(frozen=True, slots=True)
class UsageLedger:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Durable reservation, settlement, and measured-usage facts.

    Totals are derived so reserved exposure can never be mistaken for provider
    consumption. Unknown terminal cost is retained per operation instead of
    poisoning the known measured total.
    """

    reservations: tuple[ResourceReservation, ...] = ()
    active_reservation_ids: tuple[str, ...] = ()
    measurements: tuple[UsageRecord, ...] = ()
    settled_operation_ids: tuple[str, ...] = ()
    released_operation_ids: tuple[str, ...] = ()
    unknown_cost_operation_ids: tuple[str, ...] = ()
    unknown_usage_operation_ids: tuple[str, ...] = ()
    unknown_terminal_operation_ids: tuple[str, ...] = ()

    @property
    def actions(self) -> int:
        """Return measured external actions only."""
        return _known_measurement_total(
            self.measurements, lambda measurement: measurement.actions
        )

    @property
    def evaluations(self) -> int:
        """Return measured external evaluations only."""
        return _known_measurement_total(
            self.measurements, lambda measurement: measurement.evaluations
        )

    @property
    def model_calls(self) -> int:
        """Return measured model calls only."""
        return _known_measurement_total(
            self.measurements, lambda measurement: measurement.model_calls
        )

    @property
    def _supervisor_measurements(self) -> tuple[UsageRecord, ...]:
        """Return the measured model calls attributable to advisory supervision."""
        return tuple(
            record
            for record in self.measurements
            if record.operation_id.startswith(SUPERVISOR_REQUEST_PREFIX)
        )

    @property
    def _supervisor_reservations(self) -> tuple[ResourceReservation, ...]:
        """Return the reservations opened by advisory supervisor calls."""
        return tuple(
            reservation
            for reservation in self.reservations
            if reservation.operation_id.startswith(SUPERVISOR_REQUEST_PREFIX)
        )

    @property
    def supervisor_model_calls(self) -> int:
        """Return settled supervisor calls, a subset of ``model_calls``."""
        return _known_measurement_total(
            self._supervisor_measurements,
            lambda measurement: measurement.model_calls,
        )

    @property
    def conservative_supervisor_model_calls(self) -> int:
        """Return settled supervisor calls plus unresolved supervisor exposure."""
        unknown = set(self.unknown_terminal_operation_ids)
        return self.supervisor_model_calls + sum(
            reservation.model_calls or 0
            for reservation in self._supervisor_reservations
            if reservation.reservation_id in self.active_reservation_ids
            or (
                reservation.operation_id in unknown
                and reservation.operation_id not in self.released_operation_ids
            )
        )

    @property
    def supervisor_input_tokens(self) -> int | None:
        """Return known supervisor input tokens, or unknown when unresolved."""
        return self._supervisor_token_total(
            lambda measurement: measurement.input_tokens
        )

    @property
    def supervisor_output_tokens(self) -> int | None:
        """Return known supervisor output tokens, or unknown when unresolved."""
        return self._supervisor_token_total(
            lambda measurement: measurement.output_tokens
        )

    def _supervisor_token_total(
        self, extractor: Callable[[UsageMeasurement], int | None]
    ) -> int | None:
        """Sum one token dimension over supervisor calls without hiding unknowns.

        An active supervisor reservation, or any settled supervisor call missing
        the dimension, makes the total unknown; no supervisor call is known zero.
        """
        if any(
            reservation.reservation_id in self.active_reservation_ids
            for reservation in self._supervisor_reservations
        ):
            return None
        measurements = self._supervisor_measurements
        if not measurements:
            return 0
        values = tuple(extractor(record.measurement) for record in measurements)
        if any(value is None for value in values):
            return None
        return sum((value for value in values if value is not None), 0)

    @property
    def conservative_actions(self) -> int:
        """Return measured actions plus all unresolved admission exposure."""
        return self._conservative_total("actions")

    @property
    def conservative_evaluations(self) -> int:
        """Return measured evaluations plus all unresolved exposure."""
        return self._conservative_total("evaluations")

    @property
    def conservative_model_calls(self) -> int:
        """Return measured model calls plus all unresolved exposure."""
        return self._conservative_total("model_calls")

    @property
    def conservative_tokens(self) -> int:
        """Charge billed tokens or retain the bound when usage is incomplete."""
        measured: dict[str, int] = {}
        incomplete: set[str] = set()
        for record in self.measurements:
            usage = record.measurement
            if not usage.model_calls and not (
                usage.input_tokens or usage.output_tokens
            ):
                continue
            operation = record.operation_id
            if usage.input_tokens is None or usage.output_tokens is None:
                incomplete.add(operation)
            measured[operation] = (
                measured.get(operation, 0)
                + (usage.input_tokens or 0)
                + (usage.output_tokens or 0)
            )
        for reservation in self.reservations:
            operation = reservation.operation_id
            if reservation.tokens is None or operation in self.released_operation_ids:
                continue
            if operation in incomplete or operation not in measured:
                measured[operation] = max(
                    measured.get(operation, 0), reservation.tokens
                )
        return sum(measured.values())

    @property
    def input_tokens(self) -> int | None:
        """Return known input-token total, or unknown when any record lacks it."""
        return _optional_measurement_total(
            self.measurements,
            self.reservations,
            self.released_operation_ids,
            self.unknown_usage_operation_ids,
            lambda measurement: measurement.input_tokens,
        )

    @property
    def output_tokens(self) -> int | None:
        """Return known output-token total, or unknown when any record lacks it."""
        return _optional_measurement_total(
            self.measurements,
            self.reservations,
            self.released_operation_ids,
            self.unknown_usage_operation_ids,
            lambda measurement: measurement.output_tokens,
        )

    @property
    def duration_ms(self) -> int | None:
        """Return known duration total, or unknown when any record lacks it."""
        return _optional_measurement_total(
            self.measurements,
            self.reservations,
            self.released_operation_ids,
            self.unknown_usage_operation_ids,
            lambda measurement: measurement.duration_ms,
        )

    @property
    def cost(self) -> Decimal | None:
        """Return known cost total, or unknown while terminal cost is unresolved."""
        if self.unknown_cost_operation_ids:
            return None
        costs = tuple(record.measurement.cost for record in self.measurements)
        if any(cost is None for cost in costs):
            return None
        return sum((cost for cost in costs if cost is not None), Decimal("0"))

    @property
    def known_cost(self) -> Decimal:
        """Return the sum of provider-reported costs without hiding unknowns."""
        return sum(
            (record.measurement.cost or Decimal("0") for record in self.measurements),
            Decimal("0"),
        )

    @property
    def active_reservations(self) -> tuple[ResourceReservation, ...]:
        """Return reservations currently contributing admission exposure."""
        return tuple(
            reservation
            for reservation in self.reservations
            if reservation.reservation_id in self.active_reservation_ids
        )

    def _has_unknown_cost_exposure(self) -> bool:
        return bool(self.unknown_cost_operation_ids) or any(
            reservation.cost is None for reservation in self.active_reservations
        )

    def reserve(
        self, reservation: ResourceReservation, limits: BudgetLimits
    ) -> UsageLedger:
        """Refuse a reservation that exceeds a configured known resource limit."""
        if reservation.reservation_id in self.active_reservation_ids:
            raise TransitionError("reservation is already active")
        if any(
            item.reservation_id == reservation.reservation_id
            for item in self.reservations
        ):
            raise TransitionError("reservation ID is already recorded")
        if (
            limits.actions is not None
            and self._conservative_total("actions") + reservation.actions
            > limits.actions
        ):
            raise BudgetExhausted("action")
        if (
            limits.evaluations is not None
            and self._conservative_total("evaluations") + reservation.evaluations
            > limits.evaluations
        ):
            raise BudgetExhausted("evaluation")
        if (
            limits.model_calls is not None
            and self._conservative_total("model_calls") + reservation.model_calls
            > limits.model_calls
        ):
            raise BudgetExhausted("model call")
        if limits.tokens is not None and reservation.model_calls:
            if reservation.tokens is None:
                raise TransitionError("token budget requires a reservation bound")
            if self.conservative_tokens + reservation.tokens > limits.tokens:
                raise BudgetExhausted("token")
        if limits.cost is not None:
            if reservation.cost is None or self._has_unknown_cost_exposure():
                raise TransitionError("cost budget has unknown exposure")
            if (
                self.known_cost
                + sum(item.cost or Decimal("0") for item in self.active_reservations)
                + reservation.cost
                > limits.cost
            ):
                raise BudgetExhausted("cost")
        return replace(
            self,
            reservations=(*self.reservations, reservation),
            active_reservation_ids=(
                *self.active_reservation_ids,
                reservation.reservation_id,
            ),
        )

    def record_measurement(
        self, operation_id: str, measurement: UsageMeasurement
    ) -> UsageLedger:
        """Record a provider measurement once, including after terminal settlement."""
        if not any(item.operation_id == operation_id for item in self.reservations):
            raise TransitionError("measurement does not belong to a reservation")
        for record in self.measurements:
            if record.measurement.measurement_id != measurement.measurement_id:
                continue
            if (
                record.operation_id == operation_id
                and record.measurement == measurement
            ):
                return self
            raise TransitionError(
                "measurement ID was reused with different ownership or content"
            )
        unknown_operations = self.unknown_cost_operation_ids
        if measurement.cost is None:
            unknown_operations = _append_once(unknown_operations, operation_id)
        elif not any(
            record.operation_id == operation_id and record.measurement.cost is None
            for record in self.measurements
        ):
            unknown_operations = tuple(
                item for item in unknown_operations if item != operation_id
            )
        reservation = next(
            item for item in self.reservations if item.operation_id == operation_id
        )
        prior_incomplete = any(
            record.operation_id == operation_id
            and not _measurement_is_complete(record.measurement, reservation)
            for record in self.measurements
        )
        prior_resource_incomplete = any(
            record.operation_id == operation_id
            and not _resource_measurement_is_complete(record.measurement, reservation)
            for record in self.measurements
        )
        unknown_usage_operations = self.unknown_usage_operation_ids
        complete = _measurement_is_complete(measurement, reservation)
        if complete and not prior_incomplete:
            unknown_usage_operations = tuple(
                item for item in unknown_usage_operations if item != operation_id
            )
        else:
            unknown_usage_operations = _append_once(
                unknown_usage_operations, operation_id
            )
        unknown_terminal_operations = self.unknown_terminal_operation_ids
        resource_complete = _resource_measurement_is_complete(measurement, reservation)
        if resource_complete and not prior_resource_incomplete:
            unknown_terminal_operations = tuple(
                item for item in unknown_terminal_operations if item != operation_id
            )
        else:
            unknown_terminal_operations = _append_once(
                unknown_terminal_operations, operation_id
            )
        return replace(
            self,
            measurements=(*self.measurements, UsageRecord(operation_id, measurement)),
            unknown_cost_operation_ids=unknown_operations,
            unknown_usage_operation_ids=unknown_usage_operations,
            unknown_terminal_operation_ids=unknown_terminal_operations,
        )

    def settle(
        self,
        reservation: ResourceReservation,
        measurement: UsageMeasurement | None,
    ) -> UsageLedger:
        """Close reservation exposure while retaining all measured usage facts."""
        result = self
        if measurement is not None:
            result = result.record_measurement(reservation.operation_id, measurement)
        if reservation.reservation_id not in result.active_reservation_ids:
            return result
        known_cost = any(
            record.operation_id == reservation.operation_id
            and record.measurement.cost is not None
            for record in result.measurements
        )
        unknown_operations = result.unknown_cost_operation_ids
        if not known_cost and reservation.operation_id not in unknown_operations:
            unknown_operations = (*unknown_operations, reservation.operation_id)
        unknown_usage_operations = result.unknown_usage_operation_ids
        unknown_terminal_operations = result.unknown_terminal_operation_ids
        if measurement is None:
            unknown_usage_operations = _append_once(
                unknown_usage_operations, reservation.operation_id
            )
            unknown_terminal_operations = _append_once(
                unknown_terminal_operations, reservation.operation_id
            )
        return replace(
            result,
            active_reservation_ids=tuple(
                item
                for item in result.active_reservation_ids
                if item != reservation.reservation_id
            ),
            settled_operation_ids=_append_once(
                result.settled_operation_ids, reservation.operation_id
            ),
            unknown_cost_operation_ids=unknown_operations,
            unknown_usage_operation_ids=unknown_usage_operations,
            unknown_terminal_operation_ids=unknown_terminal_operations,
        )

    def _unknown_exposure(self, field: str) -> int:
        """Conservatively count reservations whose terminal use is unknown."""
        unknown = set(self.unknown_terminal_operation_ids)
        return sum(
            getattr(reservation, field) or 0
            for reservation in self.reservations
            if reservation.operation_id in unknown
            and reservation.operation_id not in self.released_operation_ids
        )

    def _conservative_total(self, field: str) -> int:
        unknown = set(self.unknown_terminal_operation_ids)
        return getattr(self, field) + sum(
            getattr(item, field) or 0
            for item in self.reservations
            if item.reservation_id in self.active_reservation_ids
            or (
                item.operation_id in unknown
                and item.operation_id not in self.released_operation_ids
            )
        )

    def release(self, reservation: ResourceReservation) -> UsageLedger:
        """Release a reservation proved not to have reached the external system."""
        return replace(
            self,
            active_reservation_ids=tuple(
                item
                for item in self.active_reservation_ids
                if item != reservation.reservation_id
            ),
            released_operation_ids=_append_once(
                self.released_operation_ids, reservation.operation_id
            ),
        )


def _append_once(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if value in values else (*values, value)


def _optional_measurement_total(
    measurements: tuple[UsageRecord, ...],
    reservations: tuple[ResourceReservation, ...],
    released_operation_ids: tuple[str, ...],
    unknown_operation_ids: tuple[str, ...],
    extractor: Callable[[UsageMeasurement], int | None],
) -> int | None:
    model_operation_ids = {
        reservation.operation_id
        for reservation in reservations
        if reservation.model_calls > 0
        and reservation.operation_id not in released_operation_ids
    }
    model_measurements = tuple(
        record
        for record in measurements
        if record.operation_id in model_operation_ids
        or _known_value(record.measurement.model_calls) > 0
    )
    relevant = (
        model_measurements
        if model_operation_ids or model_measurements
        else measurements
    )
    missing_model_operations = model_operation_ids - {
        record.operation_id for record in model_measurements
    }
    if missing_model_operations:
        return None
    if not relevant:
        return None if unknown_operation_ids else 0
    relevant_operation_ids = {record.operation_id for record in relevant}
    if any(
        operation_id in relevant_operation_ids for operation_id in unknown_operation_ids
    ):
        return None
    values = tuple(extractor(record.measurement) for record in relevant)
    if any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), 0)


def _known_value(value: int | None) -> int:
    return 0 if value is None else value


def _known_measurement_total(
    measurements: tuple[UsageRecord, ...],
    extractor: Callable[[UsageMeasurement], int | None],
) -> int:
    """Keep known measurements visible while unknown dimensions remain separate."""
    return sum(
        value
        for value in (extractor(record.measurement) for record in measurements)
        if value is not None
    )


def _measurement_is_complete(
    measurement: UsageMeasurement, reservation: ResourceReservation
) -> bool:
    """Return whether telemetry resolves every reserved resource dimension."""
    if not _resource_measurement_is_complete(measurement, reservation):
        return False
    model_relevant = (
        reservation.model_calls > 0 or _known_value(measurement.model_calls) > 0
    )
    return not model_relevant or (
        measurement.input_tokens is not None
        and measurement.output_tokens is not None
        and measurement.duration_ms is not None
    )


def _resource_measurement_is_complete(
    measurement: UsageMeasurement, reservation: ResourceReservation
) -> bool:
    """Return whether reserved resource dimensions, including cost, are known."""
    if any(
        getattr(measurement, field) is None
        for field in ("actions", "evaluations", "model_calls")
    ):
        return False
    return reservation.cost is None or measurement.cost is not None
