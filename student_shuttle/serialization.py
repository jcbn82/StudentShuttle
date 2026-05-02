"""JSON-safe serialization helpers for booking snapshots and events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from student_shuttle.booking import (
    ActorType,
    Address,
    Airport,
    BookerType,
    Booking,
    BookingState,
    Event,
    EventType,
    FareComponents,
    Money,
    ParentContact,
    PaymentStatus,
    VehicleSnapshot,
)
from student_shuttle.notifications import NotificationRecord


def booking_to_dict(booking: Booking) -> dict[str, Any]:
    return {
        "id": str(booking.id),
        "state": booking.state.value,
        "pickup_airport": booking.pickup_airport.value,
        "pickup_flight_number": booking.pickup_flight_number,
        "pickup_scheduled_arrival": _datetime_to_str(booking.pickup_scheduled_arrival),
        "destination": {
            "line1": booking.destination.line1,
            "line2": booking.destination.line2,
            "suburb": booking.destination.suburb,
            "postcode": booking.destination.postcode,
            "state": booking.destination.state,
        },
        "student_id": str(booking.student_id),
        "is_under_18": booking.is_under_18,
        "booker_id": str(booking.booker_id),
        "booker_type": booking.booker_type.value,
        "fare_amount": _money_to_dict(booking.fare_amount),
        "fare_components": {
            "base": _money_to_dict(booking.fare_components.base),
            "late_night_surcharge": _money_to_dict(
                booking.fare_components.late_night_surcharge
            ),
            "under_18_surcharge": _money_to_dict(booking.fare_components.under_18_surcharge),
        },
        "payment_status": booking.payment_status.value,
        "agent_commission_amount": _money_to_dict(booking.agent_commission_amount),
        "driver_payment_amount": _money_to_dict(booking.driver_payment_amount),
        "created_at": _datetime_to_str(booking.created_at),
        "updated_at": _datetime_to_str(booking.updated_at),
        "pickup_actual_arrival": _optional_datetime_to_str(booking.pickup_actual_arrival),
        "homestay_host_id": _optional_uuid_to_str(booking.homestay_host_id),
        "institution_id": _optional_uuid_to_str(booking.institution_id),
        "agent_id": _optional_uuid_to_str(booking.agent_id),
        "parent_contacts": [
            {
                "name": parent.name,
                "phone": parent.phone,
                "email": parent.email,
                "preferred_channel": parent.preferred_channel,
            }
            for parent in booking.parent_contacts
        ],
        "driver_id": _optional_uuid_to_str(booking.driver_id),
        "vehicle_snapshot": _vehicle_to_dict(booking.vehicle_snapshot),
        "assignment_at": _optional_datetime_to_str(booking.assignment_at),
        "met_at": _optional_datetime_to_str(booking.met_at),
        "arrived_at": _optional_datetime_to_str(booking.arrived_at),
        "closed_at": _optional_datetime_to_str(booking.closed_at),
        "cancellation_at": _optional_datetime_to_str(booking.cancellation_at),
        "handover_receipt_id": _optional_uuid_to_str(booking.handover_receipt_id),
        "special_instructions": booking.special_instructions,
        "cancellation_reason": booking.cancellation_reason,
    }


def booking_from_dict(data: dict[str, Any]) -> Booking:
    destination = data["destination"]
    fare_components = data["fare_components"]
    return Booking(
        id=UUID(data["id"]),
        state=BookingState(data["state"]),
        pickup_airport=Airport(data["pickup_airport"]),
        pickup_flight_number=data["pickup_flight_number"],
        pickup_scheduled_arrival=_datetime_from_str(data["pickup_scheduled_arrival"]),
        destination=Address(
            line1=destination["line1"],
            line2=destination.get("line2"),
            suburb=destination["suburb"],
            postcode=destination["postcode"],
            state=destination["state"],
        ),
        student_id=UUID(data["student_id"]),
        is_under_18=data["is_under_18"],
        booker_id=UUID(data["booker_id"]),
        booker_type=BookerType(data["booker_type"]),
        fare_amount=_money_from_dict(data["fare_amount"]),
        fare_components=FareComponents(
            base=_money_from_dict(fare_components["base"]),
            late_night_surcharge=_money_from_dict(
                fare_components["late_night_surcharge"]
            ),
            under_18_surcharge=_money_from_dict(fare_components["under_18_surcharge"]),
        ),
        payment_status=PaymentStatus(data["payment_status"]),
        agent_commission_amount=_money_from_dict(data["agent_commission_amount"]),
        driver_payment_amount=_money_from_dict(data["driver_payment_amount"]),
        created_at=_datetime_from_str(data["created_at"]),
        updated_at=_datetime_from_str(data["updated_at"]),
        pickup_actual_arrival=_optional_datetime_from_str(data["pickup_actual_arrival"]),
        homestay_host_id=_optional_uuid_from_str(data["homestay_host_id"]),
        institution_id=_optional_uuid_from_str(data["institution_id"]),
        agent_id=_optional_uuid_from_str(data["agent_id"]),
        parent_contacts=[
            ParentContact(
                name=parent["name"],
                phone=parent.get("phone"),
                email=parent.get("email"),
                preferred_channel=parent.get("preferred_channel"),
            )
            for parent in data["parent_contacts"]
        ],
        driver_id=_optional_uuid_from_str(data["driver_id"]),
        vehicle_snapshot=_vehicle_from_dict(data["vehicle_snapshot"]),
        assignment_at=_optional_datetime_from_str(data["assignment_at"]),
        met_at=_optional_datetime_from_str(data["met_at"]),
        arrived_at=_optional_datetime_from_str(data["arrived_at"]),
        closed_at=_optional_datetime_from_str(data["closed_at"]),
        cancellation_at=_optional_datetime_from_str(data["cancellation_at"]),
        handover_receipt_id=_optional_uuid_from_str(data["handover_receipt_id"]),
        special_instructions=data["special_instructions"],
        cancellation_reason=data["cancellation_reason"],
    )


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "booking_id": str(event.booking_id),
        "event_type": event.event_type.value,
        "actor_id": str(event.actor_id),
        "actor_type": event.actor_type.value,
        "timestamp": _datetime_to_str(event.timestamp),
        "payload": event.payload,
    }


def event_from_dict(data: dict[str, Any]) -> Event:
    return Event(
        id=UUID(data["id"]),
        booking_id=UUID(data["booking_id"]),
        event_type=EventType(data["event_type"]),
        actor_id=UUID(data["actor_id"]),
        actor_type=ActorType(data["actor_type"]),
        timestamp=_datetime_from_str(data["timestamp"]),
        payload=data["payload"],
    )


def notification_to_dict(notification: NotificationRecord) -> dict[str, Any]:
    return {
        "id": str(notification.id),
        "event_id": str(notification.event_id),
        "booking_id": str(notification.booking_id),
        "recipient_type": notification.recipient_type.value,
        "recipient_id": str(notification.recipient_id),
        "channel": notification.channel.value,
        "template": notification.template,
        "status": notification.status.value,
        "attempts": notification.attempts,
        "last_error": notification.last_error,
        "created_at": _optional_datetime_to_str(notification.created_at),
        "updated_at": _optional_datetime_to_str(notification.updated_at),
    }


def _money_to_dict(money: Money) -> dict[str, str]:
    return {"amount": str(money.amount), "currency": money.currency}


def _money_from_dict(data: dict[str, str]) -> Money:
    return Money(amount=Decimal(data["amount"]), currency=data["currency"])


def _vehicle_to_dict(vehicle: VehicleSnapshot | None) -> dict[str, str | None] | None:
    if vehicle is None:
        return None
    return {
        "make": vehicle.make,
        "model": vehicle.model,
        "plate": vehicle.plate,
        "photo_url": vehicle.photo_url,
    }


def _vehicle_from_dict(data: dict[str, str | None] | None) -> VehicleSnapshot | None:
    if data is None:
        return None
    return VehicleSnapshot(
        make=str(data["make"]),
        model=str(data["model"]),
        plate=str(data["plate"]),
        photo_url=data.get("photo_url"),
    )


def _datetime_to_str(value: datetime) -> str:
    return value.isoformat()


def _optional_datetime_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _datetime_from_str(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_datetime_from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _optional_uuid_to_str(value: UUID | None) -> str | None:
    return str(value) if value else None


def _optional_uuid_from_str(value: str | None) -> UUID | None:
    return UUID(value) if value else None
