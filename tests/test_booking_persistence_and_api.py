from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from student_shuttle.api import BookingAPIHandler
from student_shuttle.booking import BookingState, EventType
from student_shuttle.notification_worker import (
    NotificationWorker,
    RecordingDeliveryAdapter,
    SMTPDeliveryAdapter,
    WebhookDeliveryAdapter,
    create_notification_worker_from_env,
)
from student_shuttle.notifications import Channel, NotificationStatus, RecipientType
from student_shuttle.payments import LedgerEntryType
from student_shuttle.repository import SQLiteBookingRepository
from student_shuttle.service import BookingService


class BookingPersistenceAndAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteBookingRepository()
        self.service = BookingService(self.repository)
        self.notification_worker = NotificationWorker(self.repository)
        self.actor_id = str(uuid4())
        self.pickup_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
        self.driver_id = str(uuid4())

    def tearDown(self) -> None:
        self.repository.close()

    def test_service_persists_booking_snapshot_and_events(self) -> None:
        booking, created = self.service.create_booking(self._adult_booking_payload())

        loaded = self.repository.get_booking(booking.id)
        self.assertEqual(loaded.id, booking.id)
        self.assertEqual(loaded.state, BookingState.BOOKED)
        self.assertEqual(created.event_type, EventType.BOOKING_CREATED)

        booking, assigned = self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
        )

        loaded = self.repository.get_booking(booking.id)
        events = self.repository.list_events(booking.id)
        self.assertEqual(loaded.state, BookingState.ASSIGNED)
        self.assertEqual(str(loaded.driver_id), self.driver_id)
        self.assertEqual([event.event_type for event in events], [EventType.BOOKING_CREATED, EventType.DRIVER_ASSIGNED])
        self.assertEqual(assigned.event_type, EventType.DRIVER_ASSIGNED)

    def test_notification_worker_plans_idempotently_and_delivers(self) -> None:
        booking, _ = self.service.create_booking(self._adult_booking_payload())

        planned_once = self.notification_worker.plan_for_booking(str(booking.id))
        planned_twice = self.notification_worker.plan_for_booking(str(booking.id))
        delivered = self.notification_worker.deliver_pending()

        self.assertEqual(len(planned_once), 2)
        self.assertEqual(
            {notification.id for notification in planned_once},
            {notification.id for notification in planned_twice},
        )
        self.assertEqual(len(self.repository.list_notifications(booking.id)), 2)
        self.assertTrue(all(notification.status == NotificationStatus.SENT for notification in delivered))

    def test_notification_retry_after_failure(self) -> None:
        sms_adapter = RecordingDeliveryAdapter(Channel.SMS, fail_templates={"booking_confirmed"})
        worker = NotificationWorker(
            self.repository,
            adapters={
                Channel.EMAIL: RecordingDeliveryAdapter(Channel.EMAIL),
                Channel.EMAIL_DIGEST: RecordingDeliveryAdapter(Channel.EMAIL_DIGEST),
                Channel.SMS: sms_adapter,
                Channel.WHATSAPP: RecordingDeliveryAdapter(Channel.WHATSAPP),
                Channel.SLACK: RecordingDeliveryAdapter(Channel.SLACK),
            },
        )
        booking, _ = self.service.create_booking(self._adult_booking_payload())
        planned = worker.plan_for_booking(str(booking.id))
        student_sms = next(notification for notification in planned if notification.channel == Channel.SMS)

        first_attempt = worker.retry(str(student_sms.id))
        self.assertEqual(first_attempt.status, NotificationStatus.FAILED)
        self.assertEqual(first_attempt.attempts, 1)

        sms_adapter.fail_templates.clear()
        second_attempt = worker.retry(str(student_sms.id))
        self.assertEqual(second_attempt.status, NotificationStatus.SENT)
        self.assertEqual(second_attempt.attempts, 2)

    def test_notification_worker_factory_configures_provider_adapters(self) -> None:
        worker = create_notification_worker_from_env(
            self.repository,
            environ={
                "STUDENT_SHUTTLE_SMTP_HOST": "smtp.example.test",
                "STUDENT_SHUTTLE_SMTP_FROM": "ops@example.test",
                "STUDENT_SHUTTLE_SMTP_PORT": "2525",
                "STUDENT_SHUTTLE_SMTP_TLS": "false",
                "STUDENT_SHUTTLE_SMS_WEBHOOK_URL": "http://provider.example.test/sms",
                "STUDENT_SHUTTLE_SMS_WEBHOOK_TOKEN": "secret",
            },
        )

        self.assertIsInstance(worker.adapters[Channel.EMAIL], SMTPDeliveryAdapter)
        self.assertIs(worker.adapters[Channel.EMAIL], worker.adapters[Channel.EMAIL_DIGEST])
        self.assertIsInstance(worker.adapters[Channel.SMS], WebhookDeliveryAdapter)
        self.assertIsInstance(worker.adapters[Channel.WHATSAPP], RecordingDeliveryAdapter)

    def test_webhook_delivery_adapter_posts_notification_payload(self) -> None:
        received: list[dict] = []

        class TestWebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8")
                received.append(
                    {
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(raw_body),
                    }
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestWebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            booking, _ = self.service.create_booking(self._adult_booking_payload())
            notification = self.notification_worker.plan_for_booking(str(booking.id))[0]
            adapter = WebhookDeliveryAdapter(
                channel=Channel.SMS,
                url=f"http://127.0.0.1:{server.server_port}/webhook",
                bearer_token="token",
            )

            adapter.send(notification)

            self.assertEqual(received[0]["authorization"], "Bearer token")
            self.assertEqual(received[0]["body"]["id"], str(notification.id))
            self.assertEqual(received[0]["body"]["template"], notification.template)
        finally:
            server.shutdown()
            server.server_close()

    def test_eligible_drivers_filter_availability_and_blue_card(self) -> None:
        booking, _ = self.service.create_booking(self._under_18_booking_payload())
        unavailable = self.service.create_driver(self._driver_profile_payload("Unavailable Driver"))
        expired = self.service.create_driver(
            self._driver_profile_payload(
                "Expired Driver",
                blue_card_expiry=(self.pickup_at - timedelta(days=1)).isoformat(),
            )
        )
        eligible = self.service.create_driver(self._driver_profile_payload("Eligible Driver"))

        for driver in (expired, eligible):
            self.service.add_driver_availability(
                driver.id,
                {
                    "airport": "BNE",
                    "starts_at": (self.pickup_at - timedelta(hours=2)).isoformat(),
                    "ends_at": (self.pickup_at + timedelta(hours=2)).isoformat(),
                },
            )

        eligible_drivers = self.service.eligible_drivers(booking.id)

        self.assertEqual([driver.id for driver in eligible_drivers], [eligible.id])
        self.assertNotIn(unavailable.id, {driver.id for driver in eligible_drivers})

    def test_assign_by_stored_driver_requires_availability_and_persists_snapshot(self) -> None:
        booking, _ = self.service.create_booking(self._adult_booking_payload())
        driver = self.service.create_driver(self._driver_profile_payload("Dana Driver"))

        with self.assertRaisesRegex(Exception, "not available"):
            self.service.assign_driver(
                booking.id,
                {"actor_id": self.actor_id, "driver_id": str(driver.id)},
            )

        self.service.add_driver_availability(
            driver.id,
            {
                "airport": "BNE",
                "starts_at": (self.pickup_at - timedelta(hours=1)).isoformat(),
                "ends_at": (self.pickup_at + timedelta(hours=1)).isoformat(),
            },
        )
        assigned_booking, event = self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver_id": str(driver.id)},
        )

        self.assertEqual(assigned_booking.driver_id, driver.id)
        self.assertEqual(assigned_booking.vehicle_snapshot.plate, "ABC123")
        self.assertEqual(event.event_type, EventType.DRIVER_ASSIGNED)

    def test_cancellation_policy_for_booked_booking_creates_full_refund(self) -> None:
        booking, _ = self.service.create_booking(self._adult_booking_payload())

        cancelled, event, entries = self.service.cancel_booking(
            booking.id,
            {
                "actor_id": self.actor_id,
                "reason": "Plans changed",
            },
        )

        self.assertEqual(cancelled.state, BookingState.CANCELLED)
        self.assertEqual(cancelled.payment_status.value, "REFUNDED")
        self.assertEqual(event.event_type, EventType.BOOKING_CANCELLED)
        self.assertEqual([entry.entry_type for entry in entries], [LedgerEntryType.REFUND])
        self.assertEqual(entries[0].amount.amount, booking.fare_amount.amount)

    def test_cancellation_policy_for_assigned_booking_creates_partial_refund_and_driver_fee(self) -> None:
        booking, _ = self.service.create_booking(self._adult_booking_payload())
        self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
        )

        cancelled, _, entries = self.service.cancel_booking(
            booking.id,
            {
                "actor_id": self.actor_id,
                "reason": "Cancelled after assignment",
            },
        )

        self.assertEqual(cancelled.payment_status.value, "PARTIAL_REFUND")
        self.assertEqual(
            [entry.entry_type for entry in entries],
            [LedgerEntryType.REFUND, LedgerEntryType.DRIVER_CANCELLATION_FEE],
        )
        self.assertEqual(entries[0].amount.amount, booking.fare_amount.amount * Decimal("0.50"))
        self.assertEqual(entries[1].amount.amount, Decimal("25"))

    def test_cancellation_policy_for_met_booking_creates_driver_fee_only(self) -> None:
        booking, _ = self.service.create_booking(self._adult_booking_payload())
        self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
        )
        self.service.mark_met(
            booking.id,
            {"actor_id": self.actor_id, "override_flight_check": True},
        )

        cancelled, _, entries = self.service.cancel_booking(
            booking.id,
            {
                "actor_id": self.actor_id,
                "reason": "Cancelled after pickup issue",
            },
        )

        self.assertEqual(cancelled.payment_status.value, "PAID")
        self.assertEqual(
            [entry.entry_type for entry in entries],
            [LedgerEntryType.DRIVER_CANCELLATION_FEE],
        )
        self.assertEqual(entries[0].amount.amount, Decimal("50"))

    def test_api_creates_fetches_and_transitions_booking(self) -> None:
        server = self._start_test_server()
        try:
            created_status, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            self.assertEqual(created_status, 201)
            booking_id = created_body["booking"]["id"]

            fetched_status, fetched_body = self._request(
                "GET", f"/bookings/{booking_id}", None, server.server_port
            )
            self.assertEqual(fetched_status, 200)
            self.assertEqual(fetched_body["booking"]["state"], "BOOKED")

            assigned_status, assigned_body = self._request(
                "POST",
                f"/bookings/{booking_id}/assign-driver",
                {"actor_id": self.actor_id, "driver": self._driver_payload()},
                server.server_port,
            )
            self.assertEqual(assigned_status, 200)
            self.assertEqual(assigned_body["booking"]["state"], "ASSIGNED")
            self.assertEqual(assigned_body["event"]["event_type"], "driver_assigned")

            events_status, events_body = self._request(
                "GET", f"/bookings/{booking_id}/events", None, server.server_port
            )
            self.assertEqual(events_status, 200)
            self.assertEqual(
                [event["event_type"] for event in events_body["events"]],
                ["booking_created", "driver_assigned"],
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_conflict_for_invalid_transition(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]
            status, body = self._request(
                "POST",
                f"/bookings/{booking_id}/mark-met",
                {"actor_id": self.actor_id},
                server.server_port,
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "business_rule_error")
            self.assertIn("BOOKED", body["error"]["message"])
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_structured_validation_error_for_missing_field(self) -> None:
        server = self._start_test_server()
        try:
            status, body = self._request(
                "POST",
                "/bookings",
                {},
                server.server_port,
            )

            self.assertEqual(status, 400)
            self.assertEqual(
                body["error"],
                {
                    "code": "validation_error",
                    "message": "pickup_airport is required",
                    "field": "pickup_airport",
                },
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_structured_not_found_error(self) -> None:
        server = self._start_test_server()
        try:
            status, body = self._request(
                "GET",
                f"/bookings/{uuid4()}",
                None,
                server.server_port,
            )

            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            self.assertIn("booking", body["error"]["message"])
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_structured_validation_error_for_invalid_json(self) -> None:
        server = self._start_test_server()
        try:
            status, body = self._raw_request(
                "POST",
                "/bookings",
                b"{not-json",
                server.server_port,
            )

            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertEqual(body["error"]["message"], "request body must be valid JSON")
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_forbidden_for_disallowed_actor_type(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]

            status, body = self._request(
                "POST",
                f"/bookings/{booking_id}/assign-driver",
                {
                    "actor_id": self.actor_id,
                    "actor_type": "BUYER",
                    "driver": self._driver_payload(),
                },
                server.server_port,
            )

            self.assertEqual(status, 403)
            self.assertEqual(body["error"]["code"], "forbidden")
            self.assertEqual(body["error"]["field"], "actor_type")
        finally:
            server.shutdown()
            server.server_close()

    def test_api_returns_validation_error_for_unknown_actor_type(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]

            status, body = self._request(
                "POST",
                f"/bookings/{booking_id}/mark-met",
                {
                    "actor_id": self.actor_id,
                    "actor_type": "ALIEN",
                },
                server.server_port,
            )

            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertEqual(body["error"]["field"], "actor_type")
        finally:
            server.shutdown()
            server.server_close()

    def test_api_plans_lists_and_delivers_notifications(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]

            planned_status, planned_body = self._request(
                "POST",
                f"/bookings/{booking_id}/notifications/plan",
                {},
                server.server_port,
            )
            self.assertEqual(planned_status, 200)
            self.assertEqual(len(planned_body["notifications"]), 2)

            listed_status, listed_body = self._request(
                "GET",
                f"/bookings/{booking_id}/notifications",
                None,
                server.server_port,
            )
            self.assertEqual(listed_status, 200)
            self.assertEqual(
                [notification["status"] for notification in listed_body["notifications"]],
                ["pending", "pending"],
            )

            delivered_status, delivered_body = self._request(
                "POST",
                "/notifications/deliver-pending",
                {},
                server.server_port,
            )
            self.assertEqual(delivered_status, 200)
            self.assertEqual(
                {notification["status"] for notification in delivered_body["notifications"]},
                {"sent"},
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_under_18_close_requires_stored_signed_handover_receipt(self) -> None:
        booking, _ = self.service.create_booking(self._under_18_booking_payload())
        self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
        )
        self.service.mark_met(
            booking.id,
            {"actor_id": self.actor_id, "override_flight_check": True},
        )
        self.service.mark_arrived(booking.id, {"actor_id": self.actor_id})

        receipt = self.service.create_handover_receipt(
            booking.id,
            {"handover_to": "Host Parent"},
        )
        expected_retention = receipt.created_at + timedelta(days=365 * 7)
        self.assertEqual(receipt.retention_until.date(), expected_retention.date())

        with self.assertRaisesRegex(Exception, "stored handover_receipt_id"):
            self.service.close_booking(booking.id, {"actor_id": self.actor_id})

        self.service.sign_handover_receipt(
            receipt.id,
            {"signer_type": "driver"},
        )
        with self.assertRaisesRegex(Exception, "host/welfare signatures"):
            self.service.close_booking(
                booking.id,
                {"actor_id": self.actor_id, "handover_receipt_id": str(receipt.id)},
            )

        signed = self.service.sign_handover_receipt(
            receipt.id,
            {"signer_type": "host"},
        )
        self.assertTrue(signed.is_fully_signed_under_18_receipt())
        closed_booking, event = self.service.close_booking(
            booking.id,
            {"actor_id": self.actor_id, "handover_receipt_id": str(receipt.id)},
        )

        self.assertEqual(closed_booking.state, BookingState.CLOSED)
        self.assertEqual(str(closed_booking.handover_receipt_id), str(receipt.id))
        self.assertEqual(event.event_type, EventType.BOOKING_CLOSED)

    def test_api_creates_signs_and_uses_handover_receipt(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._under_18_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]
            self._request(
                "POST",
                f"/bookings/{booking_id}/assign-driver",
                {"actor_id": self.actor_id, "driver": self._driver_payload()},
                server.server_port,
            )
            self._request(
                "POST",
                f"/bookings/{booking_id}/mark-met",
                {"actor_id": self.actor_id, "override_flight_check": True},
                server.server_port,
            )
            self._request(
                "POST",
                f"/bookings/{booking_id}/mark-arrived",
                {"actor_id": self.actor_id},
                server.server_port,
            )

            receipt_status, receipt_body = self._request(
                "POST",
                f"/bookings/{booking_id}/handover-receipts",
                {"handover_to": "Host Parent"},
                server.server_port,
            )
            self.assertEqual(receipt_status, 201)
            receipt_id = receipt_body["document"]["id"]
            self.assertFalse(receipt_body["document"]["is_fully_signed"])

            for signer_type in ("driver", "welfare_officer"):
                sign_status, sign_body = self._request(
                    "POST",
                    f"/documents/{receipt_id}/sign",
                    {"signer_type": signer_type},
                    server.server_port,
                )
                self.assertEqual(sign_status, 200)
            self.assertTrue(sign_body["document"]["is_fully_signed"])

            close_status, close_body = self._request(
                "POST",
                f"/bookings/{booking_id}/close",
                {"actor_id": self.actor_id, "handover_receipt_id": receipt_id},
                server.server_port,
            )
            self.assertEqual(close_status, 200)
            self.assertEqual(close_body["booking"]["state"], "CLOSED")
            self.assertEqual(close_body["booking"]["handover_receipt_id"], receipt_id)
        finally:
            server.shutdown()
            server.server_close()

    def test_incident_lifecycle_persists_without_changing_booking_state(self) -> None:
        booking = self._arrived_under_18_booking()

        booking_after_incident, event, incident = self.service.raise_incident(
            booking.id,
            {
                "actor_id": self.actor_id,
                "actor_type": "DRIVER",
                "severity": "high",
                "description": "Student not found at designated gate",
            },
        )

        incidents = self.repository.list_incidents(booking.id)
        self.assertEqual(booking_after_incident.state, BookingState.ARRIVED)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].event_id, event.id)
        self.assertEqual(incidents[0].id, incident.id)
        self.assertEqual(incidents[0].status.value, "open")

        triaged = self.service.triage_incident(
            incidents[0].id,
            {"actor_id": self.actor_id},
        )
        self.assertEqual(triaged.status.value, "triaged")
        resolved = self.service.resolve_incident(
            incidents[0].id,
            {"actor_id": self.actor_id, "resolution_notes": "Ops confirmed student is with host."},
        )
        self.assertEqual(resolved.status.value, "resolved")
        self.assertEqual(resolved.resolution_notes, "Ops confirmed student is with host.")

    def test_under_18_incident_notification_fanout_is_planned(self) -> None:
        booking = self._arrived_under_18_booking()
        _, event, _ = self.service.raise_incident(
            booking.id,
            {
                "actor_id": self.actor_id,
                "actor_type": "DRIVER",
                "severity": "high",
                "description": "Student not found at designated gate",
            },
        )

        planned = self.notification_worker.plan_for_booking(str(booking.id))
        incident_notifications = [
            notification for notification in planned if notification.event_id == event.id
        ]
        recipient_types = {
            notification.recipient_type for notification in incident_notifications
        }
        self.assertIn(RecipientType.PARENT, recipient_types)
        self.assertIn(RecipientType.AGENT, recipient_types)
        self.assertIn(RecipientType.INSTITUTION, recipient_types)
        self.assertIn(RecipientType.OPS, recipient_types)

    def test_api_raises_triages_and_resolves_incident(self) -> None:
        server = self._start_test_server()
        try:
            booking_id = self._create_arrived_under_18_booking_via_api(server.server_port)

            incident_status, incident_body = self._request(
                "POST",
                f"/bookings/{booking_id}/incidents",
                {
                    "actor_id": self.actor_id,
                    "actor_type": "DRIVER",
                    "severity": "medium",
                    "description": "Host unreachable",
                },
                server.server_port,
            )
            self.assertEqual(incident_status, 201)
            self.assertEqual(incident_body["booking"]["state"], "ARRIVED")

            list_status, list_body = self._request(
                "GET",
                f"/bookings/{booking_id}/incidents",
                None,
                server.server_port,
            )
            self.assertEqual(list_status, 200)
            incident_id = list_body["incidents"][0]["id"]
            self.assertEqual(list_body["incidents"][0]["status"], "open")

            triage_status, triage_body = self._request(
                "POST",
                f"/incidents/{incident_id}/triage",
                {"actor_id": self.actor_id},
                server.server_port,
            )
            self.assertEqual(triage_status, 200)
            self.assertEqual(triage_body["incident"]["status"], "triaged")

            resolve_status, resolve_body = self._request(
                "POST",
                f"/incidents/{incident_id}/resolve",
                {"actor_id": self.actor_id, "resolution_notes": "Emergency contact reached."},
                server.server_port,
            )
            self.assertEqual(resolve_status, 200)
            self.assertEqual(resolve_body["incident"]["status"], "resolved")
        finally:
            server.shutdown()
            server.server_close()

    def test_api_creates_driver_lists_eligible_and_assigns_by_driver_id(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]
            driver_status, driver_body = self._request(
                "POST",
                "/drivers",
                self._driver_profile_payload("Dana Driver"),
                server.server_port,
            )
            self.assertEqual(driver_status, 201)
            driver_id = driver_body["driver"]["id"]

            availability_status, _ = self._request(
                "POST",
                f"/drivers/{driver_id}/availability",
                {
                    "airport": "BNE",
                    "starts_at": (self.pickup_at - timedelta(hours=1)).isoformat(),
                    "ends_at": (self.pickup_at + timedelta(hours=1)).isoformat(),
                },
                server.server_port,
            )
            self.assertEqual(availability_status, 201)

            eligible_status, eligible_body = self._request(
                "GET",
                f"/bookings/{booking_id}/eligible-drivers",
                None,
                server.server_port,
            )
            self.assertEqual(eligible_status, 200)
            self.assertEqual([driver["id"] for driver in eligible_body["drivers"]], [driver_id])

            assign_status, assign_body = self._request(
                "POST",
                f"/bookings/{booking_id}/assign-driver",
                {"actor_id": self.actor_id, "driver_id": driver_id},
                server.server_port,
            )
            self.assertEqual(assign_status, 200)
            self.assertEqual(assign_body["booking"]["driver_id"], driver_id)
        finally:
            server.shutdown()
            server.server_close()

    def test_api_cancel_returns_and_lists_ledger_entries(self) -> None:
        server = self._start_test_server()
        try:
            _, created_body = self._request(
                "POST",
                "/bookings",
                self._adult_booking_payload(),
                server.server_port,
            )
            booking_id = created_body["booking"]["id"]

            cancel_status, cancel_body = self._request(
                "POST",
                f"/bookings/{booking_id}/cancel",
                {"actor_id": self.actor_id, "reason": "Plans changed"},
                server.server_port,
            )
            self.assertEqual(cancel_status, 200)
            self.assertEqual(cancel_body["booking"]["payment_status"], "REFUNDED")
            self.assertEqual(
                [entry["entry_type"] for entry in cancel_body["ledger"]],
                ["refund"],
            )

            ledger_status, ledger_body = self._request(
                "GET",
                f"/bookings/{booking_id}/ledger",
                None,
                server.server_port,
            )
            self.assertEqual(ledger_status, 200)
            self.assertEqual(
                [entry["entry_type"] for entry in ledger_body["ledger"]],
                ["refund"],
            )
        finally:
            server.shutdown()
            server.server_close()

    def _start_test_server(self):
        service = self.service

        class TestHandler(BookingAPIHandler):
            pass

        TestHandler.service = service
        TestHandler.notification_worker = self.notification_worker

        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None,
        port: int,
    ) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            encoded = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            return response.status, data
        finally:
            connection.close()

    def _raw_request(
        self,
        method: str,
        path: str,
        body: bytes,
        port: int,
    ) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            return response.status, data
        finally:
            connection.close()

    def _arrived_under_18_booking(self):
        booking, _ = self.service.create_booking(self._under_18_booking_payload())
        self.service.assign_driver(
            booking.id,
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
        )
        self.service.mark_met(
            booking.id,
            {"actor_id": self.actor_id, "override_flight_check": True},
        )
        booking, _ = self.service.mark_arrived(booking.id, {"actor_id": self.actor_id})
        return booking

    def _create_arrived_under_18_booking_via_api(self, port: int) -> str:
        _, created_body = self._request(
            "POST",
            "/bookings",
            self._under_18_booking_payload(),
            port,
        )
        booking_id = created_body["booking"]["id"]
        self._request(
            "POST",
            f"/bookings/{booking_id}/assign-driver",
            {"actor_id": self.actor_id, "driver": self._driver_payload()},
            port,
        )
        self._request(
            "POST",
            f"/bookings/{booking_id}/mark-met",
            {"actor_id": self.actor_id, "override_flight_check": True},
            port,
        )
        self._request(
            "POST",
            f"/bookings/{booking_id}/mark-arrived",
            {"actor_id": self.actor_id},
            port,
        )
        return booking_id

    def _adult_booking_payload(self) -> dict:
        return {
            "pickup_airport": "BNE",
            "pickup_flight_number": "QF52",
            "pickup_scheduled_arrival": self.pickup_at.isoformat(),
            "destination": {
                "line1": "10 Student Way",
                "suburb": "Brisbane",
                "postcode": "4000",
                "state": "QLD",
            },
            "student_id": str(uuid4()),
            "is_under_18": False,
            "booker_id": str(uuid4()),
            "booker_type": "PARENT",
            "fare_amount": {"amount": "120", "currency": "AUD"},
            "fare_components": {"base": {"amount": "100", "currency": "AUD"}},
            "payment_status": "PAID",
            "agent_commission_amount": {"amount": "0", "currency": "AUD"},
            "actor_id": self.actor_id,
            "parent_contacts": [{"name": "Parent", "phone": "+8613000000000"}],
        }

    def _under_18_booking_payload(self) -> dict:
        payload = self._adult_booking_payload()
        payload.update(
            {
                "is_under_18": True,
                "booker_type": "INSTITUTION",
                "payment_status": "UNPAID",
                "institution_id": str(uuid4()),
                "homestay_host_id": str(uuid4()),
                "agent_id": str(uuid4()),
                "fare_amount": {"amount": "145", "currency": "AUD"},
                "fare_components": {
                    "base": {"amount": "100", "currency": "AUD"},
                    "under_18_surcharge": {"amount": "25", "currency": "AUD"},
                },
            }
        )
        return payload

    def _driver_payload(self) -> dict:
        return {
            "id": self.driver_id,
            "full_name": "Dana Driver",
            "phone": "+61400000000",
            "vehicle_details": {
                "make": "Toyota",
                "model": "Camry",
                "plate": "ABC123",
            },
            "blue_card_status": "CURRENT",
            "blue_card_expiry": (self.pickup_at + timedelta(days=30)).isoformat(),
            "blue_card_reference": "BC-123",
        }

    def _driver_profile_payload(
        self,
        full_name: str,
        *,
        blue_card_expiry: str | None = None,
        retention_tier: str = "gold",
    ) -> dict:
        return {
            "full_name": full_name,
            "phone": "+61400000000",
            "vehicle_details": {
                "make": "Toyota",
                "model": "Camry",
                "plate": "ABC123",
            },
            "blue_card_status": "CURRENT",
            "blue_card_expiry": blue_card_expiry
            or (self.pickup_at + timedelta(days=30)).isoformat(),
            "blue_card_reference": "BC-123",
            "retention_tier": retention_tier,
        }


if __name__ == "__main__":
    unittest.main()
