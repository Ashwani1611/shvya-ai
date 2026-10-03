# SHVYA Calendar workspace

> **Implementation snapshot:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

SHVYA Calendar now contains two sidebar sections:

- **Booking**: existing lead forms, booking pages, scheduling configuration, Google connections, reminders, and upcoming bookings. Existing URLs continue to work; `/dashboard/shvya-calendar/bookings/` is the new navigation entry.
- **Calendar**: `/dashboard/shvya-calendar/calendar/` provides day, week, and month views with a pipeline selector, date picker, Today control, and clickable appointments.

The calendar uses the organization's timezone for its grid and date boundaries. Booking details show the booking timezone explicitly. Overlapping appointments occupy separate columns; appointments spanning midnight appear on each affected day. Cancelled appointments remain visible with a strike-through status.

## Booking details and actions

Selecting an appointment opens a keyboard-accessible dialog with its time, status, meeting location/link, host, lead, current pipeline/stage, published session description, submitted responses, and authorized attachment links. Google Meet and custom URLs have Join and Copy actions; phone appointments have a Call action. A missing Google link displays the sync status instead of creating a placeholder URL.

Rescheduling loads available slots for the selected date and delegates to the existing `reschedule_booking` service. That service validates availability/capacity and updates reminders and the connected Google event. Terminal bookings cannot be rescheduled, including when their status changes after the initial read.

A pipeline move changes the linked CRM lead using `move_lead_to_pipeline_stage`, including its activity record. All of the lead's bookings therefore follow its current pipeline. The dialog explicitly describes this behavior. The booking's page, host, time, and meeting link remain unchanged. The destination pipeline and stage must be active and belong to the same organization. Existing reminder delivery continues to resolve the lead's current pipeline-bound sender.

## Access and performance

Calendar management keeps the existing organization-admin access rule. Every read and mutation is organization scoped; mutations require POST and CSRF. Detail data is Django-escaped; meeting links accept only HTTP/HTTPS. Cancellation and reschedule bearer tokens are not included in the event feed.

The feed queries only appointments overlapping the requested date range, caps each request at 42 days, and returns 500 appointments per page. The frontend follows pagination before rendering so dense periods are not silently truncated. Related data is joined, avoiding per-booking queries. Existing organization/start indexes cover the calendar range query; no schema migration is needed.

## Validation

Run `pytest apps/shvya_calendar/test_workspace.py apps/shvya_calendar/tests.py` using the repository's PostgreSQL testing settings. Workspace tests cover organization boundaries, admin access, CSRF, timezone ranges, overlap, unsafe input, pipeline moves, service reuse, terminal-state rechecks, and sidebar activation.

Manual browser checks: day/week/month navigation, overlapping appointments, mobile horizontal scrolling, pipeline filtering, Escape/focus behavior in details, copy/join links, unavailable dates, successful reschedule, and moving a lead between pipelines.

## Public booking surface

Published Calendar pages are served below `/calendar/<public_id>/<slug>/`. The current public flow supports submission, scheduling, confirmation, token-bound reschedule and token-bound cancellation. Booking writes revalidate availability and tenant/page state on the server; the browser is not the authority for slot capacity.

Google Calendar connect/disconnect stays in the authenticated Calendar workspace. Connection tokens are encrypted at rest and booking/reminder behavior remains tied to the organization and current CRM lead/pipeline state.

## Organization bookings and CRM appointment time

SHVYA owns the appointment independently of Google. A disconnected host does not
hide SHVYA slots or prevent booking. Availability still enforces working hours,
notice, buffers, blocks, and capacity. When connected, Google conflict checks
remain required; provider failures are not interpreted as free time.

`booked_at` is a fixed datetime attribute, separate from the 15 custom attributes.
Migration 0004 installs it for existing organizations and maps active bookings;
new organizations receive it automatically. Appointment time is stored in the
calendar's local timezone as an ISO datetime and displayed with AM/PM on lead cards.

- Public bookings and rescheduling update Booked at; cancelling the last active
  appointment clears it.
- Editing Booked at creates a booking on the single published booking page for
  the lead's pipeline, or reschedules its existing active booking.
- Multiple eligible pages or multiple active appointments require choosing the
  appointment in Calendar. No organization or host is guessed across tenants.
- Invalid, unavailable, or conflicting times reject the save without persisting
  a mismatched CRM value. Cancel in Calendar before clearing an active booking.
- Manual bookings use the page duration, timezone, meeting location and reminders.
  Google event writes are queued after commit. Google Meet must be selected and
  the organization's selected host must authorize Google Calendar.
- Each booking has its own stable Google event ID and conference request ID.
  Retries recover the original event; rescheduling keeps the appointment's link.
- Reconnecting a host queues upcoming disconnected appointments. Beat recovers
  pending creates and reschedules after broker outages. Reminder content is
  rendered again at dispatch so newly generated links are included.

Validation for this change: calendar service, workspace, provider throttling and
attribute-sync regression tests. Local supplemental tests used SQLite without
PostgreSQL-specific search indexes; PostgreSQL migrations/locking and live Google
OAuth/Meet creation require CI and an authorized integration smoke test.

### Booking actions and CRM meeting links

Calendar action forms use their HTML `action` attribute as the request URL; the
hidden `action` input is the reschedule/move command and must not shadow the URL.
Availability for rescheduling excludes the booking being edited.

`booked_at` is a default active datetime attribute for every organization. Migration
`0005_repair_default_booked_at` repairs missing/inactive definitions for existing
organizations; organization saves also ensure the definition exists. The attribute
remains protected from custom-attribute editing and deletion.

CRM lead cards, the attribute editor, and the conversation contact panel show
“Copy Meet link” and “Join meeting” immediately below Booked at when the selected
active booking has a meeting URL. Links are read from the current booking, including
Google links populated asynchronously and published custom meeting URLs. No
placeholder link is fabricated for phone, in-person, or pending Google bookings.
Cancelled bookings are excluded. Batch CRM rendering loads links in one query,
and all link lookups are scoped to the organization.
