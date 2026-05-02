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
- `POST /bookings/{booking_id}/assign-driver`
- `POST /bookings/{booking_id}/flight-update`
- `POST /bookings/{booking_id}/mark-met`
- `POST /bookings/{booking_id}/mark-arrived`
- `POST /bookings/{booking_id}/close`
- `POST /bookings/{booking_id}/cancel`
- `POST /bookings/{booking_id}/incidents`
