from __future__ import annotations

import unittest
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
    BookingState,
    Driver,
    EventType,
    FareComponents,
    InMemoryEventLog,
    Money,
    ParentContact,
    PaymentStatus,
    SignedDocument,
    VehicleSnapshot,
)
from student_shuttle.notifications import Channel, NotificationPlanner, RecipientType


DEFAULT_INSTITUTION = object()


class BookingStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event_log = InMemoryEventLog()
        self.actor_id = uuid4()
        self.pickup_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
        self.destination = Address(
            line1="10 Student Way",
            suburb="Brisbane",
            postcode="4000",
            state="QLD",
        )
        self.vehicle = VehicleSnapshot(make="Toyota", model="Camry", plate="ABC123")
        self.driver = Driver(
            id=uuid4(),
            full_name="Dana Driver",
            phone="+61400000000",
            vehicle_details=self.vehicle,
            blue_card_status="CURRENT",
            blue_card_expiry=self.pickup_at + timedelta(days=30),
            blue_card_reference="BC-123",
        )

    def test_happy_path_adult_booking_writes_one_event_per_transition(self) -> None:
        booking = self._adult_booking()

        booking.assign_driver(self.driver, event_sink=self.event_log, actor_id=self.actor_id)
        booking.record_flight_update(
            actual_arrival=self.pickup_at + timedelta(minutes=10),
            event_sink=self.event_log,
            actor_id=self.actor_id,
        )
        booking.mark_met(event_sink=self.event_log, actor_id=self.actor_id)
        booking.mark_arrived(event_sink=self.event_log, actor_id=self.actor_id)
        booking.close(
            event_sink=self.event_log,
            actor_id=self.actor_id,
            grace_period_elapsed=True,
            driver_payment_amount=Money.aud("85"),
        )

        self.assertEqual(booking.state, BookingState.CLOSED)
        self.assertIsNotNone(booking.assignment_at)
        self.assertIsNotNone(booking.met_at)
        self.assertIsNotNone(booking.arrived_at)
        self.assertIsNotNone(booking.closed_at)
        self.assertEqual(booking.driver_payment_amount.amount, Decimal("85"))
        self.assertEqual(
            [event.event_type for event in self.event_log.events],
            [
                EventType.BOOKING_CREATED,
                EventType.DRIVER_ASSIGNED,
                EventType.FLIGHT_UPDATED,
                EventType.DRIVER_MET_STUDENT,
                EventType.DRIVER_ARRIVED_AT_DESTINATION,
                EventType.BOOKING_CLOSED,
            ],
        )

    def test_under_18_requires_institution_and_valid_blue_card(self) -> None:
        with self.assertRaisesRegex(BookingRuleError, "institution_id"):
            self._under_18_booking(institution_id=None)

        booking = self._under_18_booking()
        expired_driver = Driver(
            id=uuid4(),
            full_name="Expired Driver",
            phone="+61400000001",
            vehicle_details=self.vehicle,
            blue_card_status="CURRENT",
            blue_card_expiry=self.pickup_at - timedelta(days=1),
        )

        with self.assertRaisesRegex(BookingRuleError, "Blue Card"):
            booking.assign_driver(expired_driver, event_sink=self.event_log, actor_id=self.actor_id)

        self.assertEqual(booking.state, BookingState.BOOKED)

    def test_mark_met_requires_landed_flight_or_override(self) -> None:
        booking = self._adult_booking()
        booking.assign_driver(self.driver, event_sink=self.event_log, actor_id=self.actor_id)

        with self.assertRaisesRegex(BookingRuleError, "flight must have landed"):
            booking.mark_met(event_sink=self.event_log, actor_id=self.actor_id)

        event = booking.mark_met(
            event_sink=self.event_log,
            actor_id=self.actor_id,
            override_flight_check=True,
        )

        self.assertEqual(booking.state, BookingState.MET)
        self.assertEqual(event.event_type, EventType.DRIVER_MET_STUDENT)

    def test_under_18_close_requires_signed_handover_receipt(self) -> None:
        booking = self._under_18_booking()
        booking.assign_driver(self.driver, event_sink=self.event_log, actor_id=self.actor_id)
        booking.mark_met(
            event_sink=self.event_log,
            actor_id=self.actor_id,
            override_flight_check=True,
        )
        booking.mark_arrived(event_sink=self.event_log, actor_id=self.actor_id)

        unsigned = SignedDocument(
            id=uuid4(),
            type="handover_receipt",
            booking_id=booking.id,
            created_at=datetime.now(timezone.utc),
            signed_by_driver_at=datetime.now(timezone.utc),
        )
        with self.assertRaisesRegex(BookingRuleError, "host/welfare signatures"):
            booking.close(event_sink=self.event_log, actor_id=self.actor_id, handover_receipt=unsigned)

        signed = SignedDocument(
            id=uuid4(),
            type="handover_receipt",
            booking_id=booking.id,
            created_at=datetime.now(timezone.utc),
            signed_by_driver_at=datetime.now(timezone.utc),
            signed_by_host_at=datetime.now(timezone.utc),
        )
        booking.close(event_sink=self.event_log, actor_id=self.actor_id, handover_receipt=signed)

        self.assertEqual(booking.state, BookingState.CLOSED)
        self.assertEqual(booking.handover_receipt_id, signed.id)

    def test_cancellation_is_not_allowed_after_arrival(self) -> None:
        booking = self._adult_booking()
        booking.assign_driver(self.driver, event_sink=self.event_log, actor_id=self.actor_id)
        booking.mark_met(
            event_sink=self.event_log,
            actor_id=self.actor_id,
            override_flight_check=True,
        )
        booking.mark_arrived(event_sink=self.event_log, actor_id=self.actor_id)

        with self.assertRaisesRegex(BookingRuleError, "ARRIVED"):
            booking.cancel(
                reason="Too late",
                event_sink=self.event_log,
                actor_id=self.actor_id,
                actor_type=ActorType.OPS,
            )

    def test_incident_is_side_branch_and_notifies_under_18_stakeholders(self) -> None:
        planner = NotificationPlanner()
        booking = self._under_18_booking()
        booking.assign_driver(self.driver, event_sink=self.event_log, actor_id=self.actor_id)
        booking.mark_met(
            event_sink=self.event_log,
            actor_id=self.actor_id,
            override_flight_check=True,
        )

        event = booking.raise_incident(
            severity="high",
            description="Student not found at designated gate",
            event_sink=self.event_log,
            actor_id=self.actor_id,
            actor_type=ActorType.DRIVER,
        )

        self.assertEqual(booking.state, BookingState.MET)
        notifications = planner.plan(booking, event)
        recipient_types = {notification.recipient_type for notification in notifications}
        self.assertIn(RecipientType.PARENT, recipient_types)
        self.assertIn(RecipientType.AGENT, recipient_types)
        self.assertIn(RecipientType.INSTITUTION, recipient_types)
        self.assertIn(RecipientType.OPS, recipient_types)

    def test_notification_fanout_and_idempotency_key(self) -> None:
        planner = NotificationPlanner()
        booking = self._under_18_booking()
        event = self.event_log.events[-1]

        notifications = planner.plan(booking, event)
        templates_by_recipient = {
            notification.recipient_type: notification.template
            for notification in notifications
        }

        self.assertEqual(templates_by_recipient[RecipientType.STUDENT], "booking_confirmed")
        self.assertEqual(templates_by_recipient[RecipientType.INSTITUTION], "booking_confirmed_under_18")
        parent = next(
            notification
            for notification in notifications
            if notification.recipient_type == RecipientType.PARENT
        )
        self.assertEqual(parent.channel, Channel.SMS)
        self.assertEqual(parent.idempotency_key, (event.id, parent.recipient_id, Channel.SMS))

    def _adult_booking(self) -> Booking:
        return Booking.create(
            pickup_airport=Airport.BNE,
            pickup_flight_number="QF52",
            pickup_scheduled_arrival=self.pickup_at,
            destination=self.destination,
            student_id=uuid4(),
            is_under_18=False,
            booker_id=uuid4(),
            booker_type=BookerType.PARENT,
            fare_amount=Money.aud("120"),
            fare_components=FareComponents(base=Money.aud("100")),
            payment_status=PaymentStatus.PAID,
            agent_commission_amount=Money.aud("0"),
            driver_payment_amount=Money.aud("0"),
            event_sink=self.event_log,
            actor_id=self.actor_id,
            parent_contacts=[ParentContact(name="Parent", phone="+8613000000000")],
        )

    def _under_18_booking(self, institution_id: UUID | None | object = DEFAULT_INSTITUTION) -> Booking:
        if institution_id is DEFAULT_INSTITUTION:
            institution_id = uuid4()
        return Booking.create(
            pickup_airport=Airport.BNE,
            pickup_flight_number="QF52",
            pickup_scheduled_arrival=self.pickup_at,
            destination=self.destination,
            student_id=uuid4(),
            is_under_18=True,
            booker_id=uuid4(),
            booker_type=BookerType.INSTITUTION,
            fare_amount=Money.aud("145"),
            fare_components=FareComponents(
                base=Money.aud("100"),
                under_18_surcharge=Money.aud("25"),
            ),
            payment_status=PaymentStatus.UNPAID,
            agent_commission_amount=Money.aud("10"),
            driver_payment_amount=Money.aud("0"),
            event_sink=self.event_log,
            actor_id=self.actor_id,
            institution_id=institution_id,
            agent_id=uuid4(),
            homestay_host_id=uuid4(),
            parent_contacts=[
                ParentContact(
                    name="Parent",
                    phone="+8613000000000",
                    email="parent@example.test",
                    preferred_channel="sms",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
