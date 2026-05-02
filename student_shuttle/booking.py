"""Booking state machine for Student Shuttle transfers.

The module is intentionally framework-free: it models the domain rules that can
later be called from API handlers, jobs, or admin workflows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Protocol
from uuid import UUID, uuid4


class BookingState(str, Enum):
    BOOKED = "BOOKED"
    ASSIGNED = "ASSIGNED"
    MET = "MET"
    ARRIVED = "ARRIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class Airport(str, Enum):
    BNE = "BNE"
    OOL = "OOL"
    SYD = "SYD"
    MEL = "MEL"
    ADL = "ADL"
    PER = "PER"


class BookerType(str, Enum):
    INSTITUTION = "INSTITUTION"
    AGENT = "AGENT"
    PARENT = "PARENT"
    STUDENT_SELF = "STUDENT_SELF"


class PaymentStatus(str, Enum):
    UNPAID = "UNPAID"
    PAID = "PAID"
    REFUNDED = "REFUNDED"
    PARTIAL_REFUND = "PARTIAL_REFUND"


class ActorType(str, Enum):
    DRIVER = "DRIVER"
    OPS = "OPS"
    SYSTEM = "SYSTEM"
    BUYER = "BUYER"
    HOST = "HOST"


class EventType(str, Enum):
    BOOKING_CREATED = "booking_created"
    DRIVER_ASSIGNED = "driver_assigned"
    DRIVER_REASSIGNED = "driver_reassigned"
    DRIVER_MET_STUDENT = "driver_met_student"
    DRIVER_ARRIVED_AT_DESTINATION = "driver_arrived_at_destination"
    BOOKING_CLOSED = "booking_closed"
    BOOKING_CANCELLED = "booking_cancelled"
    INCIDENT_RAISED = "incident_raised"
    FLIGHT_UPDATED = "flight_updated"


class BookingRuleError(ValueError):
    """Raised when a requested booking transition violates the state machine."""


@dataclass(frozen=True)
class Address:
    line1: str
    suburb: str
    postcode: str
    state: str
    line2: str | None = None


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = "AUD"

    @classmethod
    def aud(cls, amount: str | Decimal | int) -> "Money":
        return cls(amount=Decimal(amount), currency="AUD")


@dataclass(frozen=True)
class FareComponents:
    base: Money
    late_night_surcharge: Money = field(default_factory=lambda: Money.aud("0"))
    under_18_surcharge: Money = field(default_factory=lambda: Money.aud("0"))


@dataclass(frozen=True)
class ParentContact:
    name: str
    phone: str | None = None
    email: str | None = None
    preferred_channel: str | None = None


@dataclass(frozen=True)
class VehicleSnapshot:
    make: str
    model: str
    plate: str
    photo_url: str | None = None


@dataclass(frozen=True)
class Driver:
    id: UUID
    full_name: str
    phone: str
    vehicle_details: VehicleSnapshot
    blue_card_status: str | None = None
    blue_card_expiry: datetime | None = None
    blue_card_reference: str | None = None
    retention_tier: str | None = None

    def has_valid_blue_card_for(self, pickup_at: datetime) -> bool:
        return (
            self.blue_card_status == "CURRENT"
            and self.blue_card_expiry is not None
            and self.blue_card_expiry >= pickup_at
        )


@dataclass(frozen=True)
class SignedDocument:
    id: UUID
    type: str
    booking_id: UUID
    created_at: datetime
    signed_by_driver_at: datetime | None = None
    signed_by_host_at: datetime | None = None
    signed_by_welfare_officer_at: datetime | None = None

    def satisfies_under_18_handover(self) -> bool:
        host_or_welfare_signed = (
            self.signed_by_host_at is not None
            or self.signed_by_welfare_officer_at is not None
        )
        return self.signed_by_driver_at is not None and host_or_welfare_signed


@dataclass(frozen=True)
class Event:
    booking_id: UUID
    event_type: EventType
    actor_id: UUID
    actor_type: ActorType
    payload: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventSink(Protocol):
    def append(self, event: Event) -> Event:
        ...


class InMemoryEventLog:
    """Append-only event log useful for tests and in-process prototypes."""

    def __init__(self, events: Iterable[Event] | None = None) -> None:
        self._events: list[Event] = list(events or [])

    def append(self, event: Event) -> Event:
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)


@dataclass
class Booking:
    id: UUID
    state: BookingState
    pickup_airport: Airport
    pickup_flight_number: str
    pickup_scheduled_arrival: datetime
    destination: Address
    student_id: UUID
    is_under_18: bool
    booker_id: UUID
    booker_type: BookerType
    fare_amount: Money
    fare_components: FareComponents
    payment_status: PaymentStatus
    agent_commission_amount: Money
    driver_payment_amount: Money
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pickup_actual_arrival: datetime | None = None
    homestay_host_id: UUID | None = None
    institution_id: UUID | None = None
    agent_id: UUID | None = None
    parent_contacts: list[ParentContact] = field(default_factory=list)
    driver_id: UUID | None = None
    vehicle_snapshot: VehicleSnapshot | None = None
    assignment_at: datetime | None = None
    met_at: datetime | None = None
    arrived_at: datetime | None = None
    closed_at: datetime | None = None
    cancellation_at: datetime | None = None
    handover_receipt_id: UUID | None = None
    special_instructions: str = ""
    cancellation_reason: str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name == "is_under_18"
            and "is_under_18" in self.__dict__
            and value != self.__dict__["is_under_18"]
        ):
            raise BookingRuleError("is_under_18 is immutable after booking creation")
        super().__setattr__(name, value)

    @classmethod
    def create(
        cls,
        *,
        pickup_airport: Airport,
        pickup_flight_number: str,
        pickup_scheduled_arrival: datetime,
        destination: Address,
        student_id: UUID,
        is_under_18: bool,
        booker_id: UUID,
        booker_type: BookerType,
        fare_amount: Money,
        fare_components: FareComponents,
        payment_status: PaymentStatus,
        agent_commission_amount: Money,
        driver_payment_amount: Money,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.BUYER,
        institution_id: UUID | None = None,
        agent_id: UUID | None = None,
        homestay_host_id: UUID | None = None,
        parent_contacts: list[ParentContact] | None = None,
        special_instructions: str = "",
        now: datetime | None = None,
    ) -> "Booking":
        if payment_status not in {PaymentStatus.PAID, PaymentStatus.UNPAID}:
            raise BookingRuleError("booking creation requires a payable status")
        if payment_status == PaymentStatus.UNPAID and booker_type != BookerType.INSTITUTION:
            raise BookingRuleError("non-institutional bookings must be paid before BOOKED")
        if is_under_18 and institution_id is None:
            raise BookingRuleError("under-18 bookings require an institution_id")
        if not all(
            [
                pickup_flight_number,
                destination.line1,
                destination.suburb,
                destination.postcode,
                destination.state,
            ]
        ):
            raise BookingRuleError("booking is missing required pickup or destination fields")

        created_at = now or datetime.now(timezone.utc)
        booking = cls(
            id=uuid4(),
            state=BookingState.BOOKED,
            pickup_airport=pickup_airport,
            pickup_flight_number=pickup_flight_number,
            pickup_scheduled_arrival=pickup_scheduled_arrival,
            destination=destination,
            student_id=student_id,
            is_under_18=is_under_18,
            booker_id=booker_id,
            booker_type=booker_type,
            fare_amount=fare_amount,
            fare_components=fare_components,
            payment_status=payment_status,
            agent_commission_amount=agent_commission_amount,
            driver_payment_amount=driver_payment_amount,
            created_at=created_at,
            updated_at=created_at,
            institution_id=institution_id,
            agent_id=agent_id,
            homestay_host_id=homestay_host_id,
            parent_contacts=parent_contacts or [],
            special_instructions=special_instructions,
        )
        booking._emit(
            event_sink,
            EventType.BOOKING_CREATED,
            actor_id,
            actor_type,
            {"booker_type": booker_type.value, "is_under_18": is_under_18},
            created_at,
        )
        return booking

    def assign_driver(
        self,
        driver: Driver,
        *,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.OPS,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.BOOKED, BookingState.ASSIGNED)
        if self.is_under_18 and not driver.has_valid_blue_card_for(
            self.pickup_scheduled_arrival
        ):
            raise BookingRuleError("under-18 assignment requires a current Blue Card")

        assigned_at = now or datetime.now(timezone.utc)
        previous_driver_id = self.driver_id
        self.driver_id = driver.id
        self.vehicle_snapshot = driver.vehicle_details
        self.assignment_at = assigned_at
        self.state = BookingState.ASSIGNED
        self._touch(assigned_at)

        if previous_driver_id is None:
            event_type = EventType.DRIVER_ASSIGNED
            payload: dict[str, Any] = {"driver_id": str(driver.id)}
        else:
            event_type = EventType.DRIVER_REASSIGNED
            payload = {
                "driver_id": str(driver.id),
                "previous_driver_id": str(previous_driver_id),
            }
        return self._emit(event_sink, event_type, actor_id, actor_type, payload, assigned_at)

    def mark_met(
        self,
        *,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.DRIVER,
        override_flight_check: bool = False,
        check_in_photo_url: str | None = None,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.ASSIGNED)
        if self.driver_id is None or self.assignment_at is None:
            raise BookingRuleError("booking must have an assigned driver before MET")
        if self.pickup_actual_arrival is None and not override_flight_check:
            raise BookingRuleError("flight must have landed or be explicitly overridden")

        met_at = now or datetime.now(timezone.utc)
        self.met_at = met_at
        self.state = BookingState.MET
        self._touch(met_at)
        payload = {"check_in_photo_url": check_in_photo_url} if check_in_photo_url else {}
        return self._emit(
            event_sink,
            EventType.DRIVER_MET_STUDENT,
            actor_id,
            actor_type,
            payload,
            met_at,
        )

    def mark_arrived(
        self,
        *,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.DRIVER,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.MET)
        arrived_at = now or datetime.now(timezone.utc)
        self.arrived_at = arrived_at
        self.state = BookingState.ARRIVED
        self._touch(arrived_at)
        return self._emit(
            event_sink,
            EventType.DRIVER_ARRIVED_AT_DESTINATION,
            actor_id,
            actor_type,
            {},
            arrived_at,
        )

    def close(
        self,
        *,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.DRIVER,
        handover_receipt: SignedDocument | None = None,
        grace_period_elapsed: bool = False,
        driver_payment_amount: Money | None = None,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.ARRIVED)

        if self.is_under_18:
            if handover_receipt is None:
                raise BookingRuleError("under-18 closure requires a handover receipt")
            if handover_receipt.booking_id != self.id:
                raise BookingRuleError("handover receipt belongs to a different booking")
            if not handover_receipt.satisfies_under_18_handover():
                raise BookingRuleError("under-18 receipt requires driver and host/welfare signatures")
            self.handover_receipt_id = handover_receipt.id
        elif not grace_period_elapsed and handover_receipt is None:
            raise BookingRuleError("adult closure requires handover action or elapsed grace period")
        elif handover_receipt is not None:
            self.handover_receipt_id = handover_receipt.id

        closed_at = now or datetime.now(timezone.utc)
        if driver_payment_amount is not None:
            self.driver_payment_amount = driver_payment_amount
        self.closed_at = closed_at
        self.state = BookingState.CLOSED
        self._touch(closed_at)
        return self._emit(
            event_sink,
            EventType.BOOKING_CLOSED,
            actor_id,
            actor_type,
            {
                "handover_receipt_id": str(self.handover_receipt_id)
                if self.handover_receipt_id
                else None
            },
            closed_at,
        )

    def cancel(
        self,
        *,
        reason: str,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.BUYER,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.BOOKED, BookingState.ASSIGNED, BookingState.MET)
        cancelled_at = now or datetime.now(timezone.utc)
        self.cancellation_reason = reason
        self.cancellation_at = cancelled_at
        self.state = BookingState.CANCELLED
        self._touch(cancelled_at)
        return self._emit(
            event_sink,
            EventType.BOOKING_CANCELLED,
            actor_id,
            actor_type,
            {"reason": reason},
            cancelled_at,
        )

    def raise_incident(
        self,
        *,
        severity: str,
        description: str,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType,
        now: datetime | None = None,
    ) -> Event:
        self._require_state(BookingState.MET, BookingState.ARRIVED)
        raised_at = now or datetime.now(timezone.utc)
        self._touch(raised_at)
        return self._emit(
            event_sink,
            EventType.INCIDENT_RAISED,
            actor_id,
            actor_type,
            {"severity": severity, "description": description},
            raised_at,
        )

    def record_flight_update(
        self,
        *,
        actual_arrival: datetime,
        event_sink: EventSink,
        actor_id: UUID,
        actor_type: ActorType = ActorType.SYSTEM,
        now: datetime | None = None,
    ) -> Event:
        event_at = now or datetime.now(timezone.utc)
        self.pickup_actual_arrival = actual_arrival
        self._touch(event_at)
        return self._emit(
            event_sink,
            EventType.FLIGHT_UPDATED,
            actor_id,
            actor_type,
            {"pickup_actual_arrival": actual_arrival.isoformat()},
            event_at,
        )

    def _require_state(self, *allowed: BookingState) -> None:
        if self.state not in allowed:
            allowed_states = ", ".join(state.value for state in allowed)
            raise BookingRuleError(
                f"booking state {self.state.value} cannot perform this action; "
                f"expected one of: {allowed_states}"
            )

    def _touch(self, updated_at: datetime) -> None:
        self.updated_at = updated_at

    def _emit(
        self,
        event_sink: EventSink,
        event_type: EventType,
        actor_id: UUID,
        actor_type: ActorType,
        payload: dict[str, Any],
        timestamp: datetime,
    ) -> Event:
        event = Event(
            booking_id=self.id,
            event_type=event_type,
            actor_id=actor_id,
            actor_type=actor_type,
            payload=payload,
            timestamp=timestamp,
        )
        return event_sink.append(event)
