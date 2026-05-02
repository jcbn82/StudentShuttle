# StudentShuttle

Backend domain code for the Student Shuttle booking flow.

## Booking state machine

The `student_shuttle` package contains a framework-free implementation of the
booking state machine described in the attached specification:

- guarded transitions from `BOOKED` through `ASSIGNED`, `MET`, `ARRIVED`, and
  `CLOSED`, plus pre-arrival cancellation;
- one append-only event emitted per state transition;
- under-18 rules for institution linkage, Blue Card validation, and signed
  handover receipt closure;
- notification fanout planning derived from events.

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Local API

Run the dependency-free JSON API with:

```bash
python3 -m student_shuttle.api
```

It starts on `http://127.0.0.1:8000` and stores data in
`student_shuttle.sqlite3` by default.

Implemented lifecycle routes:

- `POST /bookings`
- `GET /bookings/{booking_id}`
- `GET /bookings/{booking_id}/events`
- `GET /bookings/{booking_id}/notifications`
- `GET /bookings/{booking_id}/documents`
- `POST /bookings/{booking_id}/assign-driver`
- `POST /bookings/{booking_id}/flight-update`
- `POST /bookings/{booking_id}/mark-met`
- `POST /bookings/{booking_id}/mark-arrived`
- `POST /bookings/{booking_id}/close`
- `POST /bookings/{booking_id}/cancel`
- `POST /bookings/{booking_id}/incidents`
- `POST /bookings/{booking_id}/notifications/plan`
- `POST /bookings/{booking_id}/handover-receipts`
- `POST /notifications/deliver-pending`
- `POST /notifications/{notification_id}/retry`
- `POST /documents/{document_id}/sign`

## Notifications

Notifications are derived from persisted booking events. Planning creates one
notification record per `(event_id, recipient_id, channel)` idempotency key, so
running the planner repeatedly will not duplicate delivery intents.

The local notification worker uses recording adapters for email, email digest,
SMS, WhatsApp, and Slack. These adapters make delivery state testable without
external providers; production adapters can implement the same `send` method.

## Under-18 handover receipts

Under-18 bookings must close with a persisted handover receipt. Create a receipt
after arrival, sign it as the driver and either the host or welfare officer, then
close the booking with `handover_receipt_id`. Receipt documents include payload
metadata and a seven-year retention date.
