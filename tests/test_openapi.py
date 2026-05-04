from __future__ import annotations

import json
import unittest
from pathlib import Path


class OpenAPITests(unittest.TestCase):
    def setUp(self) -> None:
        spec_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
        self.spec = json.loads(spec_path.read_text(encoding="utf-8"))

    def test_openapi_spec_covers_implemented_routes(self) -> None:
        expected_routes = {
            ("post", "/bookings"),
            ("get", "/bookings/{booking_id}"),
            ("get", "/bookings/{booking_id}/events"),
            ("get", "/bookings/{booking_id}/notifications"),
            ("get", "/bookings/{booking_id}/documents"),
            ("get", "/bookings/{booking_id}/incidents"),
            ("get", "/bookings/{booking_id}/ledger"),
            ("get", "/bookings/{booking_id}/eligible-drivers"),
            ("post", "/bookings/{booking_id}/assign-driver"),
            ("post", "/bookings/{booking_id}/flight-update"),
            ("post", "/bookings/{booking_id}/mark-met"),
            ("post", "/bookings/{booking_id}/mark-arrived"),
            ("post", "/bookings/{booking_id}/close"),
            ("post", "/bookings/{booking_id}/cancel"),
            ("post", "/bookings/{booking_id}/incidents"),
            ("post", "/bookings/{booking_id}/notifications/plan"),
            ("post", "/bookings/{booking_id}/handover-receipts"),
            ("post", "/notifications/deliver-pending"),
            ("post", "/notifications/{notification_id}/retry"),
            ("post", "/documents/{document_id}/sign"),
            ("post", "/incidents/{incident_id}/triage"),
            ("post", "/incidents/{incident_id}/resolve"),
            ("post", "/drivers"),
            ("get", "/drivers/{driver_id}"),
            ("post", "/drivers/{driver_id}/availability"),
        }
        actual_routes = {
            (method, path)
            for path, methods in self.spec["paths"].items()
            for method in methods
        }

        self.assertEqual(actual_routes, expected_routes)

    def test_openapi_spec_defines_core_schemas_and_errors(self) -> None:
        schemas = self.spec["components"]["schemas"]
        for schema_name in [
            "Booking",
            "Event",
            "Notification",
            "Document",
            "Incident",
            "Driver",
            "DriverAvailability",
            "LedgerEntry",
            "APIError",
        ]:
            self.assertIn(schema_name, schemas)

        responses = self.spec["components"]["responses"]
        self.assertIn("ValidationError", responses)
        self.assertIn("NotFoundError", responses)
        self.assertIn("BusinessRuleError", responses)


if __name__ == "__main__":
    unittest.main()
