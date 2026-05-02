from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from uuid import uuid4

from student_shuttle.api import BookingAPIHandler
from student_shuttle.booking import BookingState, EventType
from student_shuttle.notification_worker import NotificationWorker, RecordingDeliveryAdapter
from student_shuttle.notifications import Channel, NotificationStatus, RecipientType
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

    def test_api_returns_bad_request_for_invalid_transition(self) -> None:
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
            self.assertEqual(status, 400)
            self.assertIn("BOOKED", body["error"])
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


if __name__ == "__main__":
    unittest.main()
