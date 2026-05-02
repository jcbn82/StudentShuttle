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
