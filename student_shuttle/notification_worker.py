"""Notification planning and delivery worker."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Protocol
from urllib import request

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


@dataclass
class SMTPDeliveryAdapter:
    """SMTP-backed email adapter using only the Python standard library."""

    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    timeout_seconds: int = 10

    def send(self, notification: NotificationRecord) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = str(notification.recipient_id)
        message["Subject"] = f"Student Shuttle: {notification.template}"
        message.set_content(_notification_body(notification))

        if self.use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=context,
            ) as smtp:
                self._login_if_configured(smtp)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
            ) as smtp:
                self._login_if_configured(smtp)
                smtp.send_message(message)

    def _login_if_configured(self, smtp: smtplib.SMTP) -> None:
        if self.username and self.password:
            smtp.login(self.username, self.password)


@dataclass
class WebhookDeliveryAdapter:
    """HTTP webhook adapter suitable for SMS, WhatsApp, Slack, or provider shims."""

    channel: Channel
    url: str
    bearer_token: str | None = None
    timeout_seconds: int = 10

    def send(self, notification: NotificationRecord) -> None:
        payload = json.dumps(
            {
                "id": str(notification.id),
                "event_id": str(notification.event_id),
                "booking_id": str(notification.booking_id),
                "recipient_type": notification.recipient_type.value,
                "recipient_id": str(notification.recipient_id),
                "channel": notification.channel.value,
                "template": notification.template,
                "body": _notification_body(notification),
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        http_request = request.Request(self.url, data=payload, headers=headers, method="POST")
        with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            status = getattr(response, "status", response.getcode())
            if status >= 400:
                raise RuntimeError(f"{self.channel.value} webhook returned HTTP {status}")


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


def create_notification_worker_from_env(
    repository: SQLiteBookingRepository,
    *,
    environ: dict[str, str] | None = None,
) -> NotificationWorker:
    """Build a notification worker from environment configuration.

    Unconfigured channels fall back to recording adapters so local development
    stays dependency-free and safe.
    """

    env = environ if environ is not None else os.environ
    adapters: dict[Channel, DeliveryAdapter] = _recording_adapters()

    smtp_host = env.get("STUDENT_SHUTTLE_SMTP_HOST")
    smtp_sender = env.get("STUDENT_SHUTTLE_SMTP_FROM")
    if smtp_host and smtp_sender:
        email_adapter = SMTPDeliveryAdapter(
            host=smtp_host,
            port=int(env.get("STUDENT_SHUTTLE_SMTP_PORT", "465")),
            sender=smtp_sender,
            username=env.get("STUDENT_SHUTTLE_SMTP_USERNAME"),
            password=env.get("STUDENT_SHUTTLE_SMTP_PASSWORD"),
            use_tls=_env_bool(env.get("STUDENT_SHUTTLE_SMTP_TLS", "true")),
        )
        adapters[Channel.EMAIL] = email_adapter
        adapters[Channel.EMAIL_DIGEST] = email_adapter

    webhook_configs = {
        Channel.SMS: ("STUDENT_SHUTTLE_SMS_WEBHOOK_URL", "STUDENT_SHUTTLE_SMS_WEBHOOK_TOKEN"),
        Channel.WHATSAPP: (
            "STUDENT_SHUTTLE_WHATSAPP_WEBHOOK_URL",
            "STUDENT_SHUTTLE_WHATSAPP_WEBHOOK_TOKEN",
        ),
        Channel.SLACK: (
            "STUDENT_SHUTTLE_SLACK_WEBHOOK_URL",
            "STUDENT_SHUTTLE_SLACK_WEBHOOK_TOKEN",
        ),
    }
    for channel, (url_key, token_key) in webhook_configs.items():
        if env.get(url_key):
            adapters[channel] = WebhookDeliveryAdapter(
                channel=channel,
                url=env[url_key],
                bearer_token=env.get(token_key),
            )

    return NotificationWorker(repository, adapters=adapters)


def _recording_adapters() -> dict[Channel, DeliveryAdapter]:
    return {
        Channel.EMAIL: RecordingDeliveryAdapter(Channel.EMAIL),
        Channel.EMAIL_DIGEST: RecordingDeliveryAdapter(Channel.EMAIL_DIGEST),
        Channel.SMS: RecordingDeliveryAdapter(Channel.SMS),
        Channel.WHATSAPP: RecordingDeliveryAdapter(Channel.WHATSAPP),
        Channel.SLACK: RecordingDeliveryAdapter(Channel.SLACK),
    }


def _env_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _notification_body(notification: NotificationRecord) -> str:
    return (
        f"Template: {notification.template}\n"
        f"Booking: {notification.booking_id}\n"
        f"Recipient type: {notification.recipient_type.value}\n"
        f"Recipient: {notification.recipient_id}\n"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
