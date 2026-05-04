"""Document records for booking artefacts such as handover receipts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from student_shuttle.booking import SignedDocument


class DocumentType(str, Enum):
    HANDOVER_RECEIPT = "handover_receipt"


@dataclass
class DocumentRecord:
    booking_id: UUID
    type: DocumentType
    created_at: datetime
    retention_until: datetime
    payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    file_ref: str | None = None
    signed_by_driver_at: datetime | None = None
    signed_by_host_at: datetime | None = None
    signed_by_welfare_officer_at: datetime | None = None
    updated_at: datetime | None = None

    def to_signed_document(self) -> SignedDocument:
        return SignedDocument(
            id=self.id,
            type=self.type.value,
            booking_id=self.booking_id,
            created_at=self.created_at,
            signed_by_driver_at=self.signed_by_driver_at,
            signed_by_host_at=self.signed_by_host_at,
            signed_by_welfare_officer_at=self.signed_by_welfare_officer_at,
        )

    def is_fully_signed_under_18_receipt(self) -> bool:
        return self.to_signed_document().satisfies_under_18_handover()
