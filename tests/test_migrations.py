from __future__ import annotations

import sqlite3
import unittest

from student_shuttle.migrations import MIGRATIONS, run_migrations
from student_shuttle.repository import SQLiteBookingRepository


class MigrationTests(unittest.TestCase):
    def test_run_migrations_creates_expected_tables(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            run_migrations(connection)

            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

            self.assertEqual(
                {
                    "bookings",
                    "events",
                    "notifications",
                    "documents",
                    "incidents",
                    "drivers",
                    "driver_availability",
                    "ledger_entries",
                    "schema_migrations",
                },
                table_names,
            )
            applied = connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                applied,
                [(migration.version, migration.name) for migration in MIGRATIONS],
            )
        finally:
            connection.close()

    def test_run_migrations_is_idempotent(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            run_migrations(connection)
            run_migrations(connection)

            applied_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]

            self.assertEqual(applied_count, len(MIGRATIONS))
        finally:
            connection.close()

    def test_repository_initialization_runs_migrations(self) -> None:
        repository = SQLiteBookingRepository()
        try:
            applied_count = repository.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]

            self.assertEqual(applied_count, len(MIGRATIONS))
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
