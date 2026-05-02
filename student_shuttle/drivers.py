"""Driver profiles and availability windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from student_shuttle.booking import Airport, Driver, VehicleSnapshot


@dataclass
class DriverRecord:
    full_name: str
    phone: str
    vehicle_details: VehicleSnapshot
    blue_card_status: str | None = None
    blue_card_expiry: datetime | None = None
    blue_card_reference: str | None = None
    retention_tier: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_domain_driver(self) -> Driver:
        return Driver(
            id=self.id,
            full_name=self.full_name,
            phone=self.phone,
            vehicle_details=self.vehicle_details,
            blue_card_status=self.blue_card_status,
            blue_card_expiry=self.blue_card_expiry,
            blue_card_reference=self.blue_card_reference,
            retention_tier=self.retention_tier,
        )


@dataclass
class DriverAvailability:
    driver_id: UUID
    airport: Airport
    starts_at: datetime
    ends_at: datetime
    id: UUID = field(default_factory=uuid4)
    created_at: datetime | None = None

    def covers(self, airport: Airport, pickup_at: datetime) -> bool:
        return self.airport == airport and self.starts_at <= pickup_at <= self.ends_at
