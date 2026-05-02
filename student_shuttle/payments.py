"""Payment ledger records for booking financial movements."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from student_shuttle.booking import Money


class LedgerEntryType(str, Enum):
    FARE_CHARGED = "fare_charged"
    REFUND = "refund"
    DRIVER_CANCELLATION_FEE = "driver_cancellation_fee"
    DRIVER_PAYMENT = "driver_payment"
    AGENT_COMMISSION = "agent_commission"


@dataclass
class LedgerEntry:
    booking_id: UUID
    event_id: UUID
    entry_type: LedgerEntryType
    amount: Money
    description: str
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
