"""Local document storage and rendering helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from student_shuttle.documents import DocumentRecord, DocumentType


@dataclass
class LocalDocumentStore:
    """Filesystem-backed document store for generated booking artefacts."""

    root: Path

    @classmethod
    def from_env(cls) -> "LocalDocumentStore":
        configured_root = os.environ.get("STUDENT_SHUTTLE_DOCUMENT_STORE_DIR")
        return cls(Path(configured_root or "var/documents"))

    def save_handover_receipt(self, document: DocumentRecord) -> str:
        if document.type != DocumentType.HANDOVER_RECEIPT:
            raise ValueError("only handover receipts can be rendered")
        relative_path = Path("handover_receipts") / f"{document.id}.txt"
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_handover_receipt(document), encoding="utf-8")
        return relative_path.as_posix()

    def read_text(self, file_ref: str) -> str:
        return (self.root / file_ref).read_text(encoding="utf-8")


def render_handover_receipt(document: DocumentRecord) -> str:
    payload = document.payload
    destination = payload.get("destination", {})
    destination_lines = [
        destination.get("line1"),
        destination.get("line2"),
        " ".join(
            part
            for part in [
                destination.get("suburb"),
                destination.get("state"),
                destination.get("postcode"),
            ]
            if part
        ),
    ]
    destination_text = "\n".join(line for line in destination_lines if line)
    return "\n".join(
        [
            "Student Shuttle Handover Receipt",
            f"Document ID: {document.id}",
            f"Booking ID: {payload.get('booking_id')}",
            f"Student ID: {payload.get('student_id')}",
            f"Institution ID: {payload.get('institution_id')}",
            f"Driver ID: {payload.get('driver_id')}",
            f"Vehicle plate: {payload.get('vehicle_plate')}",
            f"Arrival time: {payload.get('arrival_time')}",
            "Destination:",
            destination_text,
            f"Handover to: {payload.get('handover_to')}",
            f"Created at: {document.created_at.isoformat()}",
            f"Retention until: {document.retention_until.isoformat()}",
            f"Signed by driver at: {_optional_datetime(document.signed_by_driver_at)}",
            f"Signed by host at: {_optional_datetime(document.signed_by_host_at)}",
            "Signed by welfare officer at: "
            f"{_optional_datetime(document.signed_by_welfare_officer_at)}",
            "",
        ]
    )


def _optional_datetime(value) -> str:
    return value.isoformat() if value else "pending"
