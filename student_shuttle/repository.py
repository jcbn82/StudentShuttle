"""SQLite persistence for bookings and their append-only event log."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable
from uuid import UUID

from student_shuttle.booking import Booking, Event, EventSink
from student_shuttle.serialization import booking_from_dict, booking_to_dict, event_from_dict, event_to_dict


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

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return event_from_dict(data)
