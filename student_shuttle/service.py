"""Application service layer for persisted booking state changes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from student_shuttle.booking import (
    ActorType,
    Address,
    Airport,
    BookerType,
    Booking,
    BookingRuleError,
    Driver,
    Event,
    FareComponents,
    Money,
    ParentContact,
    PaymentStatus,
    SignedDocument,
    VehicleSnapshot,
)
from student_shuttle.document_storage import LocalDocumentStore
from student_shuttle.documents import DocumentRecord, DocumentType
from student_shuttle.drivers import DriverAvailability, DriverRecord
from student_shuttle.incidents import IncidentRecord, IncidentStatus
from student_shuttle.payments import LedgerEntry, LedgerEntryType
from student_shuttle.repository import SQLiteBookingRepository


class BookingService:
    """Coordinates domain transitions with durable booking/event persistence."""

    def __init__(
        self,
        repository: SQLiteBookingRepository,
        *,
        document_store: LocalDocumentStore | None = None,
    ) -> None:
        self.repository = repository
        self.document_store = document_store or LocalDocumentStore.from_env()

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
        driver = (
            self.repository.get_driver(data["driver_id"]).to_domain_driver()
            if data.get("driver_id")
            else _driver_from_data(data["driver"])
        )
        if data.get("driver_id") and not self.repository.driver_is_available(
            driver.id,
            booking.pickup_airport,
            booking.pickup_scheduled_arrival,
        ):
            raise BookingRuleError("driver is not available for this booking pickup")
        event = booking.assign_driver(
            driver,
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.OPS.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def create_driver(self, data: dict) -> DriverRecord:
        now = _parse_optional_datetime(data.get("now")) or _utc_now()
        driver = DriverRecord(
            id=_optional_uuid(data.get("id")) or uuid4(),
            full_name=data["full_name"],
            phone=data["phone"],
            vehicle_details=_vehicle_from_data(data["vehicle_details"]),
            blue_card_status=data.get("blue_card_status"),
            blue_card_expiry=_parse_optional_datetime(data.get("blue_card_expiry")),
            blue_card_reference=data.get("blue_card_reference"),
            retention_tier=data.get("retention_tier"),
            created_at=now,
            updated_at=now,
        )
        return self.repository.save_driver(driver)

    def add_driver_availability(
        self, driver_id: UUID | str, data: dict
    ) -> DriverAvailability:
        self.repository.get_driver(driver_id)
        starts_at = _parse_datetime(data["starts_at"])
        ends_at = _parse_datetime(data["ends_at"])
        if ends_at < starts_at:
            raise ValueError("availability ends_at must be after starts_at")
        availability = DriverAvailability(
            driver_id=UUID(str(driver_id)),
            airport=Airport(data["airport"]),
            starts_at=starts_at,
            ends_at=ends_at,
            created_at=_parse_optional_datetime(data.get("created_at")) or _utc_now(),
        )
        return self.repository.save_driver_availability(availability)

    def eligible_drivers(self, booking_id: UUID | str) -> tuple[DriverRecord, ...]:
        booking = self.repository.get_booking(booking_id)
        eligible = []
        for driver in self.repository.list_drivers():
            if not self.repository.driver_is_available(
                driver.id,
                booking.pickup_airport,
                booking.pickup_scheduled_arrival,
            ):
                continue
            if booking.is_under_18 and not driver.to_domain_driver().has_valid_blue_card_for(
                booking.pickup_scheduled_arrival
            ):
                continue
            eligible.append(driver)
        tier_priority = {"gold": 0, "silver": 1, "bronze": 2}
        return tuple(
            sorted(
                eligible,
                key=lambda driver: (
                    tier_priority.get((driver.retention_tier or "").lower(), 99),
                    driver.full_name,
                    str(driver.id),
                ),
            )
        )

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
        handover_receipt = None
        if data.get("handover_receipt_id"):
            handover_receipt = self.repository.get_document(
                data["handover_receipt_id"]
            ).to_signed_document()
        elif booking.is_under_18:
            raise BookingRuleError("under-18 closure requires a stored handover_receipt_id")
        elif data.get("handover_receipt"):
            handover_receipt = _document_from_data(data["handover_receipt"])
        event = booking.close(
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.DRIVER.value)),
            handover_receipt=handover_receipt,
            grace_period_elapsed=bool(data.get("grace_period_elapsed", False)),
            driver_payment_amount=_money_from_data(data["driver_payment_amount"])
            if data.get("driver_payment_amount")
            else None,
            now=_parse_optional_datetime(data.get("now")),
        )
        self.repository.save_booking(booking)
        return booking, event

    def create_handover_receipt(self, booking_id: UUID | str, data: dict) -> DocumentRecord:
        booking = self.repository.get_booking(booking_id)
        if not booking.is_under_18:
            raise BookingRuleError("handover receipts are only required for under-18 bookings")
        if booking.arrived_at is None:
            raise BookingRuleError("handover receipt requires booking to be ARRIVED")
        created_at = _parse_optional_datetime(data.get("created_at")) or _utc_now()
        retention_until = created_at + timedelta(days=365 * 7)
        payload = {
            "booking_id": str(booking.id),
            "student_id": str(booking.student_id),
            "institution_id": str(booking.institution_id) if booking.institution_id else None,
            "driver_id": str(booking.driver_id) if booking.driver_id else None,
            "vehicle_plate": booking.vehicle_snapshot.plate if booking.vehicle_snapshot else None,
            "arrival_time": booking.arrived_at.isoformat() if booking.arrived_at else None,
            "destination": {
                "line1": booking.destination.line1,
                "line2": booking.destination.line2,
                "suburb": booking.destination.suburb,
                "postcode": booking.destination.postcode,
                "state": booking.destination.state,
            },
            "handover_to": data.get("handover_to"),
        }
        document = DocumentRecord(
            booking_id=booking.id,
            type=DocumentType.HANDOVER_RECEIPT,
            file_ref=data.get("file_ref"),
            payload=payload,
            retention_until=retention_until,
            created_at=created_at,
            updated_at=created_at,
        )
        if document.file_ref is None:
            document.file_ref = self.document_store.save_handover_receipt(document)
        return self.repository.save_document(document)

    def sign_handover_receipt(self, document_id: UUID | str, data: dict) -> DocumentRecord:
        document = self.repository.get_document(document_id)
        signed_at = _parse_optional_datetime(data.get("signed_at")) or _utc_now()
        signer_type = data["signer_type"]
        if signer_type == "driver":
            document.signed_by_driver_at = signed_at
        elif signer_type == "host":
            document.signed_by_host_at = signed_at
        elif signer_type == "welfare_officer":
            document.signed_by_welfare_officer_at = signed_at
        else:
            raise ValueError("signer_type must be driver, host, or welfare_officer")
        document.updated_at = signed_at
        if document.file_ref:
            self.document_store.save_handover_receipt(document)
        return self.repository.save_document(document)

    def cancel_booking(
        self, booking_id: UUID | str, data: dict
    ) -> tuple[Booking, Event, tuple[LedgerEntry, ...]]:
        booking = self.repository.get_booking(booking_id)
        state_before_cancel = booking.state
        event = booking.cancel(
            reason=data["reason"],
            event_sink=self.repository,
            actor_id=UUID(data["actor_id"]),
            actor_type=ActorType(data.get("actor_type", ActorType.BUYER.value)),
            now=_parse_optional_datetime(data.get("now")),
        )
        ledger_entries = self._apply_cancellation_policy(
            booking,
            event,
            state_before_cancel,
        )
        self.repository.save_booking(booking)
        return booking, event, ledger_entries

    def _apply_cancellation_policy(
        self,
        booking: Booking,
        event: Event,
        state_before_cancel,
    ) -> tuple[LedgerEntry, ...]:
        entries: list[LedgerEntry] = []
        if state_before_cancel.value == "BOOKED":
            entries.append(
                self._ledger_entry(
                    booking,
                    event,
                    LedgerEntryType.REFUND,
                    booking.fare_amount,
                    "Full refund for cancellation before driver assignment",
                )
            )
            booking.payment_status = PaymentStatus.REFUNDED
        elif state_before_cancel.value == "ASSIGNED":
            refund = _percentage_money(booking.fare_amount, Decimal("0.50"))
            entries.append(
                self._ledger_entry(
                    booking,
                    event,
                    LedgerEntryType.REFUND,
                    refund,
                    "Partial refund for cancellation after driver assignment",
                )
            )
            entries.append(
                self._ledger_entry(
                    booking,
                    event,
                    LedgerEntryType.DRIVER_CANCELLATION_FEE,
                    Money.aud("25"),
                    "Driver cancellation fee after assignment",
                )
            )
            booking.payment_status = PaymentStatus.PARTIAL_REFUND
        elif state_before_cancel.value == "MET":
            entries.append(
                self._ledger_entry(
                    booking,
                    event,
                    LedgerEntryType.DRIVER_CANCELLATION_FEE,
                    Money.aud("50"),
                    "Driver cancellation fee after pickup",
                )
            )
        for entry in entries:
            self.repository.save_ledger_entry(entry)
        return tuple(entries)

    @staticmethod
    def _ledger_entry(
        booking: Booking,
        event: Event,
        entry_type: LedgerEntryType,
        amount: Money,
        description: str,
    ) -> LedgerEntry:
        return LedgerEntry(
            booking_id=booking.id,
            event_id=event.id,
            entry_type=entry_type,
            amount=amount,
            description=description,
            created_at=event.timestamp,
        )

    def raise_incident(
        self, booking_id: UUID | str, data: dict
    ) -> tuple[Booking, Event, IncidentRecord]:
        booking = self.repository.get_booking(booking_id)
        actor_id = UUID(data["actor_id"])
        actor_type = ActorType(data["actor_type"])
        event = booking.raise_incident(
            severity=data["severity"],
            description=data["description"],
            event_sink=self.repository,
            actor_id=actor_id,
            actor_type=actor_type,
            now=_parse_optional_datetime(data.get("now")),
        )
        incident = IncidentRecord(
            booking_id=booking.id,
            event_id=event.id,
            severity=data["severity"],
            description=data["description"],
            raised_by=actor_id,
            raised_by_type=actor_type,
            raised_at=event.timestamp,
            updated_at=event.timestamp,
        )
        self.repository.save_incident(incident)
        self.repository.save_booking(booking)
        return booking, event, incident

    def triage_incident(self, incident_id: UUID | str, data: dict) -> IncidentRecord:
        incident = self.repository.get_incident(incident_id)
        triaged_at = _parse_optional_datetime(data.get("triaged_at")) or _utc_now()
        incident.status = IncidentStatus.TRIAGED
        incident.triaged_by = UUID(data["actor_id"])
        incident.triaged_at = triaged_at
        incident.updated_at = triaged_at
        return self.repository.save_incident(incident)

    def resolve_incident(self, incident_id: UUID | str, data: dict) -> IncidentRecord:
        incident = self.repository.get_incident(incident_id)
        resolved_at = _parse_optional_datetime(data.get("resolved_at")) or _utc_now()
        incident.status = IncidentStatus.RESOLVED
        if incident.triaged_at is None:
            incident.triaged_at = resolved_at
            incident.triaged_by = UUID(data["actor_id"])
        incident.resolution_notes = data["resolution_notes"]
        incident.resolved_by = UUID(data["actor_id"])
        incident.resolved_at = resolved_at
        incident.updated_at = resolved_at
        return self.repository.save_incident(incident)


def _address_from_data(data: dict) -> Address:
    return Address(
        line1=data["line1"],
        suburb=data["suburb"],
        postcode=data["postcode"],
        state=data["state"],
        line2=data.get("line2"),
    )


def _driver_from_data(data: dict) -> Driver:
    return Driver(
        id=UUID(data["id"]),
        full_name=data["full_name"],
        phone=data["phone"],
        vehicle_details=_vehicle_from_data(data["vehicle_details"]),
        blue_card_status=data.get("blue_card_status"),
        blue_card_expiry=_parse_optional_datetime(data.get("blue_card_expiry")),
        blue_card_reference=data.get("blue_card_reference"),
        retention_tier=data.get("retention_tier"),
    )


def _vehicle_from_data(data: dict) -> VehicleSnapshot:
    return VehicleSnapshot(
        make=data["make"],
        model=data["model"],
        plate=data["plate"],
        photo_url=data.get("photo_url"),
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


def _percentage_money(money: Money, percentage: Decimal) -> Money:
    return Money(amount=(money.amount * percentage).quantize(Decimal("0.01")), currency=money.currency)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _optional_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
