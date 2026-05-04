"""Incident records for booking side-branch workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from student_shuttle.booking import ActorType


class IncidentStatus(str, Enum):
    OPEN = "open"
    TRIAGED = "triaged"
    RESOLVED = "resolved"


@dataclass
class IncidentRecord:
    booking_id: UUID
    event_id: UUID
    severity: str
    description: str
    raised_by: UUID
    raised_by_type: ActorType
    raised_at: datetime
    status: IncidentStatus = IncidentStatus.OPEN
    id: UUID = field(default_factory=uuid4)
    triaged_by: UUID | None = None
    triaged_at: datetime | None = None
    resolution_notes: str | None = None
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None
    updated_at: datetime | None = None
