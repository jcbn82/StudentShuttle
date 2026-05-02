from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from uuid import uuid4

from student_shuttle.api import BookingAPIHandler
from student_shuttle.booking import BookingState, EventType
from student_shuttle.repository import SQLiteBookingRepository
from student_shuttle.serialization import booking_to_dict
from student_shuttle.service import BookingService


class BookingPersistenceAndAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteBookingRepository()
        self.service = BookingService(self.repository)
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

    def _start_test_server(self):
        service = self.service

        class TestHandler(BookingAPIHandler):
            pass

        TestHandler.service = service

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
