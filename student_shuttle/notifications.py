"""Notification fanout rules derived from booking events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from student_shuttle.booking import Booking, Event, EventType


class RecipientType(str, Enum):
    STUDENT = "student"
    PARENT = "parent"
    AGENT = "agent"
    INSTITUTION = "institution"
    HOMESTAY_HOST = "homestay_host"
    DRIVER = "driver"
    OPS = "ops"


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    EMAIL_DIGEST = "email_digest"
    SLACK = "slack"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class Notification:
    event_id: UUID
    booking_id: UUID
    recipient_type: RecipientType
    recipient_id: UUID | str
    channel: Channel
    template: str

    @property
    def idempotency_key(self) -> tuple[UUID, UUID | str, Channel]:
        return (self.event_id, self.recipient_id, self.channel)


@dataclass
class NotificationRecord:
    event_id: UUID
    booking_id: UUID
    recipient_type: RecipientType
    recipient_id: UUID | str
    channel: Channel
    template: str
    status: NotificationStatus = NotificationStatus.PENDING
    attempts: int = 0
    id: UUID = field(default_factory=uuid4)
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_notification(
        cls,
        notification: Notification,
        *,
        created_at: datetime,
    ) -> "NotificationRecord":
        return cls(
            event_id=notification.event_id,
            booking_id=notification.booking_id,
            recipient_type=notification.recipient_type,
            recipient_id=notification.recipient_id,
            channel=notification.channel,
            template=notification.template,
            created_at=created_at,
            updated_at=created_at,
        )

    @property
    def idempotency_key(self) -> tuple[UUID, UUID | str, Channel]:
        return (self.event_id, self.recipient_id, self.channel)


class NotificationPlanner:
    """Builds notification intents from append-only booking events."""

    def plan(self, booking: Booking, event: Event) -> tuple[Notification, ...]:
        planners = {
            EventType.BOOKING_CREATED: self._booking_created,
            EventType.DRIVER_ASSIGNED: self._driver_assigned,
            EventType.DRIVER_REASSIGNED: self._driver_assigned,
            EventType.DRIVER_MET_STUDENT: self._driver_met_student,
            EventType.DRIVER_ARRIVED_AT_DESTINATION: self._driver_arrived,
            EventType.BOOKING_CLOSED: self._booking_closed,
            EventType.BOOKING_CANCELLED: self._booking_cancelled,
            EventType.INCIDENT_RAISED: self._incident_raised,
            EventType.FLIGHT_UPDATED: self._flight_updated,
        }
        return tuple(planners[event.event_type](booking, event))

    def _booking_created(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = [
            self._notification(event, booking, RecipientType.STUDENT, booking.student_id, Channel.SMS, "booking_confirmed"),
        ]
        notifications.extend(self._parent_notifications(booking, event, "booking_confirmed"))
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "booking_confirmed")
            )
        if booking.is_under_18 and booking.institution_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.INSTITUTION,
                    booking.institution_id,
                    Channel.EMAIL,
                    "booking_confirmed_under_18",
                )
            )
        return notifications

    def _driver_assigned(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = [
            self._notification(event, booking, RecipientType.STUDENT, booking.student_id, Channel.SMS, "driver_details"),
        ]
        notifications.extend(self._parent_notifications(booking, event, "driver_details"))
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "driver_details")
            )
        if booking.homestay_host_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.HOMESTAY_HOST,
                    booking.homestay_host_id,
                    Channel.SMS,
                    "driver_details",
                )
            )
        return notifications

    def _driver_met_student(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = self._parent_notifications(booking, event, "pickup_confirmed")
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "pickup_confirmed")
            )
        if booking.homestay_host_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.HOMESTAY_HOST,
                    booking.homestay_host_id,
                    Channel.SMS,
                    "pickup_confirmed",
                )
            )
        if booking.is_under_18 and booking.institution_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.INSTITUTION,
                    booking.institution_id,
                    Channel.EMAIL,
                    "pickup_confirmed_under_18",
                )
            )
        return notifications

    def _driver_arrived(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = self._parent_notifications(booking, event, "at_destination")
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "at_destination")
            )
        if booking.homestay_host_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.HOMESTAY_HOST,
                    booking.homestay_host_id,
                    Channel.SMS,
                    "at_destination",
                )
            )
        return notifications

    def _booking_closed(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = self._parent_notifications(booking, event, "trip_complete")
        if booking.agent_id:
            template = "booking_closed_under_18_summary" if booking.is_under_18 else "trip_complete"
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, template)
            )
        if booking.is_under_18 and booking.institution_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.INSTITUTION,
                    booking.institution_id,
                    Channel.EMAIL,
                    "booking_closed_with_receipt",
                )
            )
        return notifications

    def _booking_cancelled(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = [
            self._notification(event, booking, RecipientType.STUDENT, booking.student_id, Channel.SMS, "booking_cancelled"),
        ]
        notifications.extend(self._parent_notifications(booking, event, "booking_cancelled"))
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "booking_cancelled")
            )
        if booking.institution_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.INSTITUTION,
                    booking.institution_id,
                    Channel.EMAIL,
                    "booking_cancelled",
                )
            )
        if booking.homestay_host_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.HOMESTAY_HOST,
                    booking.homestay_host_id,
                    Channel.SMS,
                    "booking_cancelled",
                )
            )
        return notifications

    def _incident_raised(self, booking: Booking, event: Event) -> list[Notification]:
        notifications = self._parent_notifications(booking, event, "incident_raised")
        if booking.agent_id:
            notifications.append(
                self._notification(event, booking, RecipientType.AGENT, booking.agent_id, Channel.EMAIL, "incident_raised")
            )
        if booking.is_under_18 and booking.institution_id:
            notifications.append(
                self._notification(
                    event,
                    booking,
                    RecipientType.INSTITUTION,
                    booking.institution_id,
                    Channel.EMAIL,
                    "incident_raised_under_18",
                )
            )
            notifications.append(
                self._notification(event, booking, RecipientType.OPS, "on-call-ops", Channel.SLACK, "incident_raised")
            )
        return notifications

    def _flight_updated(self, booking: Booking, event: Event) -> list[Notification]:
        if booking.driver_id is None:
            return []
        return [
            self._notification(event, booking, RecipientType.DRIVER, booking.driver_id, Channel.WHATSAPP, "flight_updated")
        ]

    def _parent_notifications(self, booking: Booking, event: Event, template: str) -> list[Notification]:
        notifications: list[Notification] = []
        for index, parent in enumerate(booking.parent_contacts):
            recipient_id: UUID | str = parent.email or parent.phone or f"parent-{index}"
            channel = Channel(parent.preferred_channel) if parent.preferred_channel else Channel.WHATSAPP
            notifications.append(
                self._notification(event, booking, RecipientType.PARENT, recipient_id, channel, template)
            )
        return notifications

    @staticmethod
    def _notification(
        event: Event,
        booking: Booking,
        recipient_type: RecipientType,
        recipient_id: UUID | str,
        channel: Channel,
        template: str,
    ) -> Notification:
        return Notification(
            event_id=event.id,
            booking_id=booking.id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            channel=channel,
            template=template,
        )
