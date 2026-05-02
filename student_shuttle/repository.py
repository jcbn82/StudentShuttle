"""SQLite persistence for bookings and their append-only event log."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable
from uuid import UUID

from student_shuttle.booking import ActorType, Booking, Event, EventSink
from student_shuttle.documents import DocumentRecord, DocumentType
from student_shuttle.incidents import IncidentRecord, IncidentStatus
from student_shuttle.notifications import (
    Channel,
    NotificationRecord,
    NotificationStatus,
    RecipientType,
)
from student_shuttle.serialization import booking_from_dict, booking_to_dict, event_from_dict


class BookingNotFoundError(LookupError):
    """Raised when a booking cannot be found in persistence."""


class SQLiteBookingRepository(EventSink):
    """Persists booking snapshots and append-only events in SQLite."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self.connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                booking_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY (booking_id) REFERENCES bookings(id)
            );

            CREATE INDEX IF NOT EXISTS events_booking_timestamp_idx
                ON events (booking_id, timestamp);

            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                booking_id TEXT NOT NULL,
                recipient_type TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                template TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (event_id, recipient_id, channel),
                FOREIGN KEY (booking_id) REFERENCES bookings(id),
                FOREIGN KEY (event_id) REFERENCES events(id)
            );

            CREATE INDEX IF NOT EXISTS notifications_booking_idx
                ON notifications (booking_id, created_at);

            CREATE INDEX IF NOT EXISTS notifications_status_idx
                ON notifications (status, created_at);

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                booking_id TEXT NOT NULL,
                type TEXT NOT NULL,
                file_ref TEXT,
                payload TEXT NOT NULL,
                signed_by_driver_at TEXT,
                signed_by_host_at TEXT,
                signed_by_welfare_officer_at TEXT,
                retention_until TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (booking_id) REFERENCES bookings(id)
            );

            CREATE INDEX IF NOT EXISTS documents_booking_type_idx
                ON documents (booking_id, type, created_at);

            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                booking_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                description TEXT NOT NULL,
                raised_by TEXT NOT NULL,
                raised_by_type TEXT NOT NULL,
                raised_at TEXT NOT NULL,
                status TEXT NOT NULL,
                triaged_at TEXT,
                triaged_by TEXT,
                resolution_notes TEXT,
                resolved_at TEXT,
                resolved_by TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (booking_id) REFERENCES bookings(id),
                FOREIGN KEY (event_id) REFERENCES events(id)
            );

            CREATE INDEX IF NOT EXISTS incidents_booking_idx
                ON incidents (booking_id, raised_at);

            CREATE INDEX IF NOT EXISTS incidents_status_idx
                ON incidents (status, raised_at);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_booking(self, booking: Booking) -> None:
        data = booking_to_dict(booking)
        self.connection.execute(
            """
            INSERT INTO bookings (id, state, data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (
                str(booking.id),
                booking.state.value,
                json.dumps(data, sort_keys=True),
                booking.updated_at.isoformat(),
            ),
        )
        self.connection.commit()

    def get_booking(self, booking_id: UUID | str) -> Booking:
        row = self.connection.execute(
            "SELECT data FROM bookings WHERE id = ?",
            (str(booking_id),),
        ).fetchone()
        if row is None:
            raise BookingNotFoundError(f"booking {booking_id} was not found")
        return booking_from_dict(json.loads(row["data"]))

    def list_events(self, booking_id: UUID | str) -> tuple[Event, ...]:
        rows = self.connection.execute(
            """
            SELECT id, booking_id, event_type, actor_id, actor_type, timestamp, payload
            FROM events
            WHERE booking_id = ?
            ORDER BY timestamp ASC
            """,
            (str(booking_id),),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def append(self, event: Event) -> Event:
        self.connection.execute(
            """
            INSERT INTO events (id, booking_id, event_type, actor_id, actor_type, timestamp, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.id),
                str(event.booking_id),
                event.event_type.value,
                str(event.actor_id),
                event.actor_type.value,
                event.timestamp.isoformat(),
                json.dumps(event.payload, sort_keys=True),
            ),
        )
        self.connection.commit()
        return event

    @property
    def events(self) -> tuple[Event, ...]:
        rows = self.connection.execute(
            """
            SELECT id, booking_id, event_type, actor_id, actor_type, timestamp, payload
            FROM events
            ORDER BY timestamp ASC
            """
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def append_many(self, events: Iterable[Event]) -> None:
        for event in events:
            self.append(event)

    def save_notification(self, notification: NotificationRecord) -> NotificationRecord:
        """Persist a notification intent once per idempotency key."""
        row = self.connection.execute(
            """
            SELECT id, event_id, booking_id, recipient_type, recipient_id, channel,
                   template, status, attempts, last_error, created_at, updated_at
            FROM notifications
            WHERE event_id = ? AND recipient_id = ? AND channel = ?
            """,
            (
                str(notification.event_id),
                str(notification.recipient_id),
                notification.channel.value,
            ),
        ).fetchone()
        if row is not None:
            return self._notification_from_row(row)

        self.connection.execute(
            """
            INSERT INTO notifications (
                id, event_id, booking_id, recipient_type, recipient_id, channel,
                template, status, attempts, last_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(notification.id),
                str(notification.event_id),
                str(notification.booking_id),
                notification.recipient_type.value,
                str(notification.recipient_id),
                notification.channel.value,
                notification.template,
                notification.status.value,
                notification.attempts,
                notification.last_error,
                notification.created_at.isoformat() if notification.created_at else "",
                notification.updated_at.isoformat() if notification.updated_at else "",
            ),
        )
        self.connection.commit()
        return notification

    def list_notifications(self, booking_id: UUID | str) -> tuple[NotificationRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT id, event_id, booking_id, recipient_type, recipient_id, channel,
                   template, status, attempts, last_error, created_at, updated_at
            FROM notifications
            WHERE booking_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(booking_id),),
        ).fetchall()
        return tuple(self._notification_from_row(row) for row in rows)

    def pending_notifications(self) -> tuple[NotificationRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT id, event_id, booking_id, recipient_type, recipient_id, channel,
                   template, status, attempts, last_error, created_at, updated_at
            FROM notifications
            WHERE status IN (?, ?)
            ORDER BY created_at ASC, id ASC
            """,
            (NotificationStatus.PENDING.value, NotificationStatus.FAILED.value),
        ).fetchall()
        return tuple(self._notification_from_row(row) for row in rows)

    def get_notification(self, notification_id: UUID | str) -> NotificationRecord:
        row = self.connection.execute(
            """
            SELECT id, event_id, booking_id, recipient_type, recipient_id, channel,
                   template, status, attempts, last_error, created_at, updated_at
            FROM notifications
            WHERE id = ?
            """,
            (str(notification_id),),
        ).fetchone()
        if row is None:
            raise BookingNotFoundError(f"notification {notification_id} was not found")
        return self._notification_from_row(row)

    def update_notification_delivery(self, notification: NotificationRecord) -> NotificationRecord:
        self.connection.execute(
            """
            UPDATE notifications
            SET status = ?,
                attempts = ?,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                notification.status.value,
                notification.attempts,
                notification.last_error,
                notification.updated_at.isoformat() if notification.updated_at else "",
                str(notification.id),
            ),
        )
        self.connection.commit()
        return notification

    def save_document(self, document: DocumentRecord) -> DocumentRecord:
        self.connection.execute(
            """
            INSERT INTO documents (
                id, booking_id, type, file_ref, payload, signed_by_driver_at,
                signed_by_host_at, signed_by_welfare_officer_at, retention_until,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                file_ref = excluded.file_ref,
                payload = excluded.payload,
                signed_by_driver_at = excluded.signed_by_driver_at,
                signed_by_host_at = excluded.signed_by_host_at,
                signed_by_welfare_officer_at = excluded.signed_by_welfare_officer_at,
                retention_until = excluded.retention_until,
                updated_at = excluded.updated_at
            """,
            (
                str(document.id),
                str(document.booking_id),
                document.type.value,
                document.file_ref,
                json.dumps(document.payload, sort_keys=True),
                _optional_datetime_to_str(document.signed_by_driver_at),
                _optional_datetime_to_str(document.signed_by_host_at),
                _optional_datetime_to_str(document.signed_by_welfare_officer_at),
                document.retention_until.isoformat(),
                document.created_at.isoformat(),
                (document.updated_at or document.created_at).isoformat(),
            ),
        )
        self.connection.commit()
        return document

    def get_document(self, document_id: UUID | str) -> DocumentRecord:
        row = self.connection.execute(
            """
            SELECT id, booking_id, type, file_ref, payload, signed_by_driver_at,
                   signed_by_host_at, signed_by_welfare_officer_at, retention_until,
                   created_at, updated_at
            FROM documents
            WHERE id = ?
            """,
            (str(document_id),),
        ).fetchone()
        if row is None:
            raise BookingNotFoundError(f"document {document_id} was not found")
        return self._document_from_row(row)

    def list_documents(self, booking_id: UUID | str) -> tuple[DocumentRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT id, booking_id, type, file_ref, payload, signed_by_driver_at,
                   signed_by_host_at, signed_by_welfare_officer_at, retention_until,
                   created_at, updated_at
            FROM documents
            WHERE booking_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(booking_id),),
        ).fetchall()
        return tuple(self._document_from_row(row) for row in rows)

    def save_incident(self, incident: IncidentRecord) -> IncidentRecord:
        self.connection.execute(
            """
            INSERT INTO incidents (
                id, booking_id, event_id, severity, description, raised_by,
                raised_by_type, raised_at, status, triaged_at, triaged_by,
                resolution_notes, resolved_at, resolved_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                severity = excluded.severity,
                description = excluded.description,
                status = excluded.status,
                triaged_at = excluded.triaged_at,
                triaged_by = excluded.triaged_by,
                resolution_notes = excluded.resolution_notes,
                resolved_at = excluded.resolved_at,
                resolved_by = excluded.resolved_by,
                updated_at = excluded.updated_at
            """,
            (
                str(incident.id),
                str(incident.booking_id),
                str(incident.event_id),
                incident.severity,
                incident.description,
                str(incident.raised_by),
                incident.raised_by_type.value,
                incident.raised_at.isoformat(),
                incident.status.value,
                _optional_datetime_to_str(incident.triaged_at),
                str(incident.triaged_by) if incident.triaged_by else None,
                incident.resolution_notes,
                _optional_datetime_to_str(incident.resolved_at),
                str(incident.resolved_by) if incident.resolved_by else None,
                incident.updated_at.isoformat(),
            ),
        )
        self.connection.commit()
        return incident

    def get_incident(self, incident_id: UUID | str) -> IncidentRecord:
        row = self.connection.execute(
            """
            SELECT id, booking_id, event_id, severity, description, raised_by,
                   raised_by_type, raised_at, status, triaged_at, triaged_by,
                   resolution_notes, resolved_at, resolved_by, updated_at
            FROM incidents
            WHERE id = ?
            """,
            (str(incident_id),),
        ).fetchone()
        if row is None:
            raise BookingNotFoundError(f"incident {incident_id} was not found")
        return self._incident_from_row(row)

    def list_incidents(self, booking_id: UUID | str) -> tuple[IncidentRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT id, booking_id, event_id, severity, description, raised_by,
                   raised_by_type, raised_at, status, triaged_at, triaged_by,
                   resolution_notes, resolved_at, resolved_by, updated_at
            FROM incidents
            WHERE booking_id = ?
            ORDER BY raised_at ASC, id ASC
            """,
            (str(booking_id),),
        ).fetchall()
        return tuple(self._incident_from_row(row) for row in rows)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return event_from_dict(data)

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> NotificationRecord:
        from datetime import datetime

        data = dict(row)
        return NotificationRecord(
            id=UUID(data["id"]),
            event_id=UUID(data["event_id"]),
            booking_id=UUID(data["booking_id"]),
            recipient_type=RecipientType(data["recipient_type"]),
            recipient_id=data["recipient_id"],
            channel=Channel(data["channel"]),
            template=data["template"],
            status=NotificationStatus(data["status"]),
            attempts=int(data["attempts"]),
            last_error=data["last_error"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    @staticmethod
    def _incident_from_row(row: sqlite3.Row) -> IncidentRecord:
        from datetime import datetime

        data = dict(row)
        return IncidentRecord(
            id=UUID(data["id"]),
            booking_id=UUID(data["booking_id"]),
            event_id=UUID(data["event_id"]),
            severity=data["severity"],
            description=data["description"],
            raised_by=UUID(data["raised_by"]),
            raised_by_type=ActorType(data["raised_by_type"]),
            raised_at=datetime.fromisoformat(data["raised_at"]),
            status=IncidentStatus(data["status"]),
            triaged_at=_optional_datetime_from_str(data["triaged_at"]),
            triaged_by=UUID(data["triaged_by"]) if data["triaged_by"] else None,
            resolution_notes=data["resolution_notes"],
            resolved_at=_optional_datetime_from_str(data["resolved_at"]),
            resolved_by=UUID(data["resolved_by"]) if data["resolved_by"] else None,
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> DocumentRecord:
        from datetime import datetime

        data = dict(row)
        return DocumentRecord(
            id=UUID(data["id"]),
            booking_id=UUID(data["booking_id"]),
            type=DocumentType(data["type"]),
            file_ref=data["file_ref"],
            payload=json.loads(data["payload"]),
            signed_by_driver_at=_optional_datetime_from_str(data["signed_by_driver_at"]),
            signed_by_host_at=_optional_datetime_from_str(data["signed_by_host_at"]),
            signed_by_welfare_officer_at=_optional_datetime_from_str(
                data["signed_by_welfare_officer_at"]
            ),
            retention_until=datetime.fromisoformat(data["retention_until"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


def _optional_datetime_to_str(value) -> str | None:
    return value.isoformat() if value else None


def _optional_datetime_from_str(value: str | None):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None
