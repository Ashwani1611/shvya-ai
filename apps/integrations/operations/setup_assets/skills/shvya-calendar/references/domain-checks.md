# Calendar domain checks

## Evidence
Read booking-page config, opening hours, timezone, duration, capacity, buffers, provider sync state, reminders, existing booking state and availability verification.

## Known traps
- A displayed slot is not confirmed availability until canonical booking validation.
- Google Calendar connected does not prove busy-time sync is current.
- Booking page published does not prove share URL/submit path works.
- Reminder configuration does not prove reminder delivery.
- Timezone mismatch can make valid-looking slots wrong.
- Reschedule/cancel needs current booking state and provider sync, not only a requested timestamp.
- Duplicate confirmations can come from disconnected booking processes, not reminder logic.

## Verification
Validate configuration, verify representative booking availability, read back booking/provider sync after reschedule/status changes, and confirm reminder suppression/creation as appropriate.

## Handoffs
Integration health → integration manager; messaging reminder delivery → channel/Cadence skills; CRM attachment/linkage → CRM/lead repair.
