"""Small JSON HTTP API for the booking state machine.

The API uses only the Python standard library so it can run in this repository
without framework setup. A future web framework can wrap the same
``BookingService`` methods.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from student_shuttle.booking import BookingRuleError
from student_shuttle.notification_worker import NotificationWorker
from student_shuttle.repository import BookingNotFoundError, SQLiteBookingRepository
from student_shuttle.serialization import (
    availability_to_dict,
    booking_to_dict,
    document_to_dict,
    driver_to_dict,
    event_to_dict,
    incident_to_dict,
    notification_to_dict,
)
from student_shuttle.service import BookingService


class BookingAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler that dispatches booking lifecycle actions."""

    service: BookingService
    notification_worker: NotificationWorker

    def do_GET(self) -> None:
        try:
            if self.path.startswith("/drivers/"):
                driver_id = self.path.split("?")[0].strip("/").split("/")[1]
                driver = self.service.repository.get_driver(driver_id)
                self._write_json(HTTPStatus.OK, {"driver": driver_to_dict(driver)})
                return
            booking_id, suffix = self._parse_booking_route()
            if suffix == "":
                booking = self.service.repository.get_booking(booking_id)
                self._write_json(HTTPStatus.OK, {"booking": booking_to_dict(booking)})
                return
            if suffix == "/events":
                events = self.service.repository.list_events(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"events": [event_to_dict(event) for event in events]},
                )
                return
            if suffix == "/notifications":
                notifications = self.service.repository.list_notifications(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "notifications": [
                            notification_to_dict(notification)
                            for notification in notifications
                        ]
                    },
                )
                return
            if suffix == "/documents":
                documents = self.service.repository.list_documents(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "documents": [
                            document_to_dict(document) for document in documents
                        ]
                    },
                )
                return
            if suffix == "/incidents":
                incidents = self.service.repository.list_incidents(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "incidents": [
                            incident_to_dict(incident) for incident in incidents
                        ]
                    },
                )
                return
            if suffix == "/eligible-drivers":
                drivers = self.service.eligible_drivers(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"drivers": [driver_to_dict(driver) for driver in drivers]},
                )
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except BookingNotFoundError as exc:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            if self.path == "/bookings":
                payload = self._read_json()
                booking, event = self.service.create_booking(payload)
                self._write_transition(HTTPStatus.CREATED, booking, event)
                return
            if self.path == "/drivers":
                payload = self._read_json()
                driver = self.service.create_driver(payload)
                self._write_json(HTTPStatus.CREATED, {"driver": driver_to_dict(driver)})
                return
            if self.path.startswith("/drivers/") and self.path.endswith("/availability"):
                driver_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                availability = self.service.add_driver_availability(driver_id, payload)
                self._write_json(
                    HTTPStatus.CREATED,
                    {"availability": availability_to_dict(availability)},
                )
                return
            if self.path == "/notifications/deliver-pending":
                delivered = self.notification_worker.deliver_pending()
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "notifications": [
                            notification_to_dict(notification) for notification in delivered
                        ]
                    },
                )
                return
            if self.path.startswith("/notifications/") and self.path.endswith("/retry"):
                notification_id = self.path.split("?")[0].strip("/").split("/")[1]
                notification = self.notification_worker.retry(notification_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"notification": notification_to_dict(notification)},
                )
                return
            if self.path.startswith("/documents/") and self.path.endswith("/sign"):
                document_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                document = self.service.sign_handover_receipt(document_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"document": document_to_dict(document)},
                )
                return
            if self.path.startswith("/incidents/") and self.path.endswith("/triage"):
                incident_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                incident = self.service.triage_incident(incident_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"incident": incident_to_dict(incident)},
                )
                return
            if self.path.startswith("/incidents/") and self.path.endswith("/resolve"):
                incident_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                incident = self.service.resolve_incident(incident_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"incident": incident_to_dict(incident)},
                )
                return

            booking_id, suffix = self._parse_booking_route()
            if suffix == "/notifications/plan":
                notifications = self.notification_worker.plan_for_booking(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "notifications": [
                            notification_to_dict(notification)
                            for notification in notifications
                        ]
                    },
                )
                return
            if suffix == "/handover-receipts":
                payload = self._read_json()
                document = self.service.create_handover_receipt(booking_id, payload)
                self._write_json(
                    HTTPStatus.CREATED,
                    {"document": document_to_dict(document)},
                )
                return
            if suffix == "/incidents":
                payload = self._read_json()
                booking, event, incident = self.service.raise_incident(booking_id, payload)
                self._write_json(
                    HTTPStatus.CREATED,
                    {
                        "booking": booking_to_dict(booking),
                        "event": event_to_dict(event),
                        "incident": incident_to_dict(incident),
                    },
                )
                return
            actions: dict[str, Callable[[str, dict[str, Any]], Any]] = {
                "/assign-driver": self.service.assign_driver,
                "/flight-update": self.service.record_flight_update,
                "/mark-met": self.service.mark_met,
                "/mark-arrived": self.service.mark_arrived,
                "/close": self.service.close_booking,
                "/cancel": self.service.cancel_booking,
            }
            action = actions.get(suffix)
            if action is None:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                return

            payload = self._read_json()
            booking, event = action(booking_id, payload)
            self._write_transition(HTTPStatus.OK, booking, event)
        except BookingNotFoundError as exc:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (BookingRuleError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _parse_booking_route(self) -> tuple[str, str]:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) < 2 or parts[0] != "bookings":
            raise ValueError("expected /bookings/{booking_id}")
        booking_id = parts[1]
        suffix = "" if len(parts) == 2 else "/" + "/".join(parts[2:])
        return booking_id, suffix

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        if not raw_body:
            return {}
        data = json.loads(raw_body)
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _write_transition(self, status: HTTPStatus, booking: Any, event: Any) -> None:
        self._write_json(
            status,
            {
                "booking": booking_to_dict(booking),
                "event": event_to_dict(event),
            },
        )

    def _write_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    database_path: str = "student_shuttle.sqlite3",
) -> ThreadingHTTPServer:
    repository = SQLiteBookingRepository(database_path)
    service = BookingService(repository)
    notification_worker = NotificationWorker(repository)

    class ConfiguredBookingAPIHandler(BookingAPIHandler):
        pass

    ConfiguredBookingAPIHandler.service = service
    ConfiguredBookingAPIHandler.notification_worker = notification_worker
    return ThreadingHTTPServer((host, port), ConfiguredBookingAPIHandler)


def main() -> None:
    server = create_server()
    print("Student Shuttle API listening on http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
