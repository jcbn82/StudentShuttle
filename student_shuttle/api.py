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

from student_shuttle.booking import ActorType, BookingRuleError
from student_shuttle.notification_worker import (
    NotificationWorker,
    create_notification_worker_from_env,
)
from student_shuttle.repository import BookingNotFoundError, SQLiteBookingRepository
from student_shuttle.serialization import (
    availability_to_dict,
    booking_to_dict,
    document_to_dict,
    driver_to_dict,
    event_to_dict,
    incident_to_dict,
    ledger_entry_to_dict,
    notification_to_dict,
)
from student_shuttle.service import BookingService


class APIError(Exception):
    """Structured API error that maps cleanly to a JSON response."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


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
            if suffix == "/ledger":
                entries = self.service.repository.list_ledger_entries(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"ledger": [ledger_entry_to_dict(entry) for entry in entries]},
                )
                return
            if suffix == "/eligible-drivers":
                drivers = self.service.eligible_drivers(booking_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"drivers": [driver_to_dict(driver) for driver in drivers]},
                )
                return
            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "route not found")
        except Exception as exc:
            self._write_exception(exc)

    def do_POST(self) -> None:
        try:
            if self.path == "/bookings":
                payload = self._read_json()
                _require_fields(
                    payload,
                    [
                        "pickup_airport",
                        "pickup_flight_number",
                        "pickup_scheduled_arrival",
                        "destination",
                        "student_id",
                        "is_under_18",
                        "booker_id",
                        "booker_type",
                        "fare_amount",
                        "fare_components",
                        "payment_status",
                        "agent_commission_amount",
                        "actor_id",
                    ],
                )
                booking, event = self.service.create_booking(payload)
                self._write_transition(HTTPStatus.CREATED, booking, event)
                return
            if self.path == "/drivers":
                payload = self._read_json()
                _require_fields(payload, ["full_name", "phone", "vehicle_details"])
                _authorize_actor(payload, {ActorType.OPS}, ActorType.OPS)
                driver = self.service.create_driver(payload)
                self._write_json(HTTPStatus.CREATED, {"driver": driver_to_dict(driver)})
                return
            if self.path.startswith("/drivers/") and self.path.endswith("/availability"):
                driver_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS}, ActorType.OPS)
                availability = self.service.add_driver_availability(driver_id, payload)
                self._write_json(
                    HTTPStatus.CREATED,
                    {"availability": availability_to_dict(availability)},
                )
                return
            if self.path == "/notifications/deliver-pending":
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS, ActorType.SYSTEM}, ActorType.OPS)
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
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS, ActorType.SYSTEM}, ActorType.OPS)
                notification = self.notification_worker.retry(notification_id)
                self._write_json(
                    HTTPStatus.OK,
                    {"notification": notification_to_dict(notification)},
                )
                return
            if self.path.startswith("/documents/") and self.path.endswith("/sign"):
                document_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                _authorize_actor(
                    payload,
                    {ActorType.DRIVER, ActorType.HOST, ActorType.OPS},
                    ActorType.DRIVER,
                )
                document = self.service.sign_handover_receipt(document_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"document": document_to_dict(document)},
                )
                return
            if self.path.startswith("/incidents/") and self.path.endswith("/triage"):
                incident_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS}, ActorType.OPS)
                incident = self.service.triage_incident(incident_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"incident": incident_to_dict(incident)},
                )
                return
            if self.path.startswith("/incidents/") and self.path.endswith("/resolve"):
                incident_id = self.path.split("?")[0].strip("/").split("/")[1]
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS}, ActorType.OPS)
                incident = self.service.resolve_incident(incident_id, payload)
                self._write_json(
                    HTTPStatus.OK,
                    {"incident": incident_to_dict(incident)},
                )
                return

            booking_id, suffix = self._parse_booking_route()
            if suffix == "/notifications/plan":
                payload = self._read_json()
                _authorize_actor(payload, {ActorType.OPS, ActorType.SYSTEM}, ActorType.OPS)
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
                _authorize_actor(payload, {ActorType.OPS, ActorType.DRIVER}, ActorType.OPS)
                document = self.service.create_handover_receipt(booking_id, payload)
                self._write_json(
                    HTTPStatus.CREATED,
                    {"document": document_to_dict(document)},
                )
                return
            if suffix == "/incidents":
                payload = self._read_json()
                _authorize_actor(
                    payload,
                    {ActorType.DRIVER, ActorType.OPS, ActorType.HOST},
                    ActorType.DRIVER,
                )
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
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "route not found")

            payload = self._read_json()
            if suffix == "/assign-driver":
                _authorize_actor(payload, {ActorType.OPS}, ActorType.OPS)
            elif suffix in {"/mark-met", "/mark-arrived", "/close"}:
                _authorize_actor(payload, {ActorType.DRIVER, ActorType.OPS}, ActorType.DRIVER)
            elif suffix == "/cancel":
                _authorize_actor(payload, {ActorType.BUYER, ActorType.OPS}, ActorType.BUYER)
            elif suffix == "/flight-update":
                _authorize_actor(payload, {ActorType.SYSTEM, ActorType.OPS}, ActorType.SYSTEM)
            result = action(booking_id, payload)
            if suffix == "/cancel":
                booking, event, entries = result
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "booking": booking_to_dict(booking),
                        "event": event_to_dict(event),
                        "ledger": [ledger_entry_to_dict(entry) for entry in entries],
                    },
                )
                return
            booking, event = result
            self._write_transition(HTTPStatus.OK, booking, event)
        except Exception as exc:
            self._write_exception(exc)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _parse_booking_route(self) -> tuple[str, str]:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) < 2 or parts[0] != "bookings":
            raise APIError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "route not found",
            )
        booking_id = parts[1]
        suffix = "" if len(parts) == 2 else "/" + "/".join(parts[2:])
        return booking_id, suffix

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        if not raw_body:
            return {}
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                "request body must be valid JSON",
            ) from exc
        if not isinstance(data, dict):
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                "request body must be a JSON object",
            )
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

    def _write_exception(self, exc: Exception) -> None:
        if isinstance(exc, APIError):
            self._write_error(exc.status, exc.code, exc.message, field=exc.field)
            return
        if isinstance(exc, BookingNotFoundError):
            self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc))
            return
        if isinstance(exc, BookingRuleError):
            self._write_error(HTTPStatus.CONFLICT, "business_rule_error", str(exc))
            return
        if isinstance(exc, KeyError):
            field = str(exc).strip("'")
            self._write_error(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                f"{field} is required",
                field=field,
            )
            return
        if isinstance(exc, ValueError):
            self._write_error(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
            return
        self._write_error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            "unexpected server error",
        )

    def _write_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        field: str | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if field is not None:
            error["field"] = field
        self._write_json(status, {"error": error})


def _require_fields(payload: dict[str, Any], fields: list[str]) -> None:
    for field in fields:
        if field not in payload:
            raise APIError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                f"{field} is required",
                field=field,
            )


def _authorize_actor(
    payload: dict[str, Any],
    allowed: set[ActorType],
    default_actor_type: ActorType,
) -> ActorType:
    raw_actor_type = payload.get("actor_type", default_actor_type.value)
    try:
        actor_type = ActorType(raw_actor_type)
    except ValueError as exc:
        allowed_values = ", ".join(actor.value for actor in ActorType)
        raise APIError(
            HTTPStatus.BAD_REQUEST,
            "validation_error",
            f"actor_type must be one of: {allowed_values}",
            field="actor_type",
        ) from exc
    if actor_type not in allowed:
        allowed_values = ", ".join(sorted(actor.value for actor in allowed))
        raise APIError(
            HTTPStatus.FORBIDDEN,
            "forbidden",
            f"actor_type {actor_type.value} is not allowed for this action; allowed: {allowed_values}",
            field="actor_type",
        )
    return actor_type


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    database_path: str = "student_shuttle.sqlite3",
) -> ThreadingHTTPServer:
    repository = SQLiteBookingRepository(database_path)
    service = BookingService(repository)
    notification_worker = create_notification_worker_from_env(repository)

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
