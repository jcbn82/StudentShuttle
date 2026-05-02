"""Application service layer for persisted booking state changes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from student_shuttle.booking import (
    ActorType,
    Address,
    Airport,
    BookerType,
    Booking,
    Driver,
    Event,
    FareComponents,
    Money,
    ParentContact,
    PaymentStatus,
    SignedDocument,
    VehicleSnapshot,
)
from student_shuttle.repository import SQLiteBookingRepository


class BookingService:
    """Coordinates domain transitions with durable booking/event persistence."""

    def __init__(self, repository: SQLiteBookingRepository) -> None:
        self.repository = repository

    def create_booking(self, data: dict) -> tuple[Booking, Event]:
        booking = Booking.create(
            pickup_airport=Airport(data["pickup_airport"]),
            pickup_flight_number=data["pickup_flight_number"],
            pickup_scheduled_arrival=_parse_datetime(data["pickup_scheduled_arrival"]),
            destination=_address_from_data(data["destination"]),
            student_id=UUID(data["student_id"]),
            is_under_18=bool(data["is_under_18"]),
            booker_id=UUID(data["booker_id"]),
            booker_type=BookerType(data["booker_type"]),
            fare_amount=_money_from_data(data["fare_amount"]),
            fare_components=_fare_components_from_data(data["fare_components"]),
            payment_status=PaymentStatus(data["payment_status"]),
            agent_commission_amount=_money_from_data(data["agent_commission_amount"]),
            driver_payment_amount=_money_from_data(
                data.get("driver_payment_amount", {"amount": "0", "currency": "AUD"})
            ),
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.BUYER.value)),
            institution_id=_optional_uuid(data.get("institution_id")),
            agent_id=_optional_uuid(data.get("agent_id")),
            homestay_host_id=_optional_uuid(data.get("homestay_host_id")),
            parent_contacts=[
                ParentContact(
                    name=parent["name"],
                    phone=parent.get("phone"),
                    email=parent.get("email"),
                    preferred_channel=parent.get("preferred_channel"),
                )
                for parent in data.get("parent_contacts", [])
            ],
            special_instructions=data.get("special_instructions", ""),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, self.repository.events[-1]

    def assign_driver(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.assign_driver(
            _driver_from_data(data["driver"]),
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.OPS.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def record_flight_update(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.record_flight_update(
            actual_arrival=_parse_datetime(data["actual_arrival"]),
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.SYSTEM.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def mark_met(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.mark_met(
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.DRIVER.value)),
            override_flight_check=bool(data.get("override_flight_check", False)),
            check_in_photo_url=data.get("check_in_photo_url"),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def mark_arrived(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.mark_arrived(
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.DRIVER.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def close_booking(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.close(
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.DRIVER.value)),
            handover_receipt=_document_from_data(data["handover_receipt"])
            if data.get("handover_receipt")
            else None,
            grace_period_elapsed=bool(data.get("grace_period_elapsed", False)),
            driver_payment_amount=_money_from_data(data["driver_payment_amount"])
            if data.get("driver_payment_amount")
            else None,
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def cancel_booking(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.cancel(
            reason=data["reason"],
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.BUYER.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def raise_incident(self, booking_id: UUID | str, data: dict) -> tuple[Booking, Event]:
        booking = self.repository.get_booking(booking_id)
        event = booking.raise_incident(
            severity=data["severity"],
            description=data["description"],
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data["actor_type"]),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event


def _address_from_data(data: dict) -> Address:
    return Address(
        line1=data["line1"],
        suburb=data["suburb"],
        postcode=data["postcode"],
        state=data["state"],
        line2=data.get("line2"),
    )


def _driver_from_data(data: dict) -> Driver:
    vehicle = data["vehicle_details"]
    return Driver(
        id=UUID(data["id"]),
        full_name=data["full_name"],
        phone=data["phone"],
        vehicle_details=VehicleSnapshot(
            make=vehicle["make"],
            model=vehicle["model"],
            plate=vehicle["plate"],
            photo_url=vehicle.get("photo_url"),
        ),
        blue_card_status=data.get("blue_card_status"),
        blue_card_expiry=_parse_optional_datetime(data.get("blue_card_expiry")),
        blue_card_reference=data.get("blue_card_reference"),
        retention_tier=data.get("retention_tier"),
    )


def _document_from_data(data: dict) -> SignedDocument:
    return SignedDocument(
        id=UUID(data["id"]),
        type=data["type"],
        booking_id=UUID(data["booking_id"]),
        created_at=_parse_datetime(data["created_at"]),
        signed_by_driver_at=_parse_optional_datetime(data.get("signed_by_driver_at")),
        signed_by_host_at=_parse_optional_datetime(data.get("signed_by_host_at")),
        signed_by_welfare_officer_at=_parse_optional_datetime(
            data.get("signed_by_welfare_officer_at")
        ),
    )


def _fare_components_from_data(data: dict) -> FareComponents:
    return FareComponents(
        base=_money_from_data(data["base"]),
        late_night_surcharge=_money_from_data(
            data.get("late_night_surcharge", {"amount": "0", "currency": "AUD"})
        ),
        under_18_surcharge=_money_from_data(
            data.get("under_18_surcharge", {"amount": "0", "currency": "AUD"})
        ),
    )


def _money_from_data(data: dict) -> Money:
    return Money.aud(str(data["amount"])) if data.get("currency", "AUD") == "AUD" else Money(
        amount=Decimal(str(data["amount"])),
        currency=data["currency"],
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _optional_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
