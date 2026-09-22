# SHVYA Calendar workspace

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
