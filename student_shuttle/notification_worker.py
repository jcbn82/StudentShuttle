"""Notification planning and delivery worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from student_shuttle.notifications import (
    Channel,
    NotificationPlanner,
    NotificationRecord,
    NotificationStatus,
)
from student_shuttle.repository import SQLiteBookingRepository


class DeliveryAdapter(Protocol):
    def send(self, notification: NotificationRecord) -> None:
        ...


@dataclass
class RecordingDeliveryAdapter:
    """Test/local adapter that records outbound deliveries instead of sending."""

    channel: Channel
    sent: list[NotificationRecord] = field(default_factory=list)
    fail_templates: set[str] = field(default_factory=set)

    def send(self, notification: NotificationRecord) -> None:
        if notification.template in self.fail_templates:
            raise RuntimeError(f"{self.channel.value} delivery failed")
        self.sent.append(notification)


class NotificationWorker:
    """Plans notifications from events and delivers pending records."""

    def __init__(
        self,
        repository: SQLiteBookingRepository,
        *,
        planner: NotificationPlanner | None = None,
        adapters: dict[Channel, DeliveryAdapter] | None = None,
    ) -> None:
        self.repository = repository
        self.planner = planner or NotificationPlanner()
        self.adapters = adapters or {
            Channel.EMAIL: RecordingDeliveryAdapter(Channel.EMAIL),
            Channel.EMAIL_DIGEST: RecordingDeliveryAdapter(Channel.EMAIL_DIGEST),
            Channel.SMS: RecordingDeliveryAdapter(Channel.SMS),
            Channel.WHATSAPP: RecordingDeliveryAdapter(Channel.WHATSAPP),
            Channel.SLACK: RecordingDeliveryAdapter(Channel.SLACK),
        }

    def plan_for_booking(self, booking_id: str) -> tuple[NotificationRecord, ...]:
        booking = self.repository.get_booking(booking_id)
        planned: list[NotificationRecord] = []
        now = _utc_now()
        for event in self.repository.list_events(booking_id):
            for notification in self.planner.plan(booking, event):
                record = NotificationRecord.from_notification(notification, created_at=now)
                planned.append(self.repository.save_notification(record))
        return tuple(planned)

    def deliver_pending(self) -> tuple[NotificationRecord, ...]:
        delivered: list[NotificationRecord] = []
        for notification in self.repository.pending_notifications():
            delivered.append(self.deliver(notification))
        return tuple(delivered)

    def retry(self, notification_id: str) -> NotificationRecord:
        notification = self.repository.get_notification(notification_id)
        if notification.status == NotificationStatus.SENT:
            return notification
        return self.deliver(notification)

    def deliver(self, notification: NotificationRecord) -> NotificationRecord:
        adapter = self.adapters[notification.channel]
        notification.attempts += 1
        notification.updated_at = _utc_now()
        try:
            adapter.send(notification)
        except Exception as exc:
            notification.status = NotificationStatus.FAILED
            notification.last_error = str(exc)
        else:
            notification.status = NotificationStatus.SENT
            notification.last_error = None
        return self.repository.update_notification_delivery(notification)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
