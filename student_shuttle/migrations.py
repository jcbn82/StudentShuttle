"""Versioned SQLite migrations for Student Shuttle persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_booking_operations_schema",
        sql="""
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

        CREATE TABLE IF NOT EXISTS drivers (
            id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            vehicle_details TEXT NOT NULL,
            blue_card_status TEXT,
            blue_card_expiry TEXT,
            blue_card_reference TEXT,
            retention_tier TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS driver_availability (
            id TEXT PRIMARY KEY,
            driver_id TEXT NOT NULL,
            airport TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (driver_id) REFERENCES drivers(id)
        );

        CREATE INDEX IF NOT EXISTS driver_availability_lookup_idx
            ON driver_availability (airport, starts_at, ends_at);

        CREATE TABLE IF NOT EXISTS ledger_entries (
            id TEXT PRIMARY KEY,
            booking_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (booking_id) REFERENCES bookings(id),
            FOREIGN KEY (event_id) REFERENCES events(id)
        );

        CREATE INDEX IF NOT EXISTS ledger_booking_idx
            ON ledger_entries (booking_id, created_at);
        """,
    ),
)


def run_migrations(connection: sqlite3.Connection) -> None:
    """Apply unapplied migrations in version order."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied_versions = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    for migration in sorted(MIGRATIONS, key=lambda item: item.version):
        if migration.version in applied_versions:
            continue
        with connection:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
