---
name: shvya-calendar
description: Configure, validate and troubleshoot Shvya Calendar booking pages, availability, provider sync, reminders and booking outcomes.
---

# Shvya Calendar

Own booking configuration and booking-state operations exposed through Operations MCP.

## Workflow

1. Verify tenant and read `get_calendar_configuration` plus relevant integration lifecycle/health.
2. Validate page publication, opening hours, duration/capacity, buffers, provider token presence and reminder coverage with `validate_calendar_configuration`.
3. Apply safe page changes through `upsert_calendar_configuration` using dry-run/approval/read-back.
4. For one booking use `verify_booking` to inspect canonical state, provider sync and reminder delivery.
5. Use `reschedule_booking` only after canonical availability validation. Use `update_booking_status` for supported outcomes: cancelled, completed or no_show.
6. Confirm cancellation suppresses pending reminders and verify provider synchronization after state changes.
7. Separate configuration readiness from a real booking acceptance test.

## Guardrails

Do not invent slots from a diary or external calendar not connected to the organization. Do not mark a booking confirmed merely because a requested time is syntactically valid.

## Output

Return page/booking state, validation findings, provider sync, reminder state, applied changes and acceptance checks.
