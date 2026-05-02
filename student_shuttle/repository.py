"""SQLite persistence for bookings and their append-only event log."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable
from uuid import UUID

from student_shuttle.booking import Booking, Event, EventSink
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
