"""Read-only backend Google Calendar configuration and permission checks."""
import json
from urllib.parse import quote
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.shvya_calendar.google import (
    GOOGLE_CALENDAR_API, GoogleCalendarError, _admit_google_request,
    _headers, _response_json, _google_request, google_is_configured,
)
from apps.shvya_calendar.platform_google import platform_connection, platform_status


class Command(BaseCommand):
    help = "Check SHVYA Google Meet configuration without revealing secrets or creating bookings."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true",
                            help="Verify the configured platform Google owner, calendar access and Meet support. Creates no events.")

    def handle(self, *args, **options):
        status = platform_status()
        result = {"oauth_configured": google_is_configured(), "platform": status,
                  "organization_oauth_callback_path": reverse("shvya_calendar:google_callback"),
                  "live_checked": False, "events_created": 0}
        if options["live"]:
            if not status["enabled"] or not status["configured"]:
                self.stdout.write(json.dumps(result, indent=2))
                raise CommandError("Enable and configure the platform Google credentials before running --live.")
            try:
                connection = platform_connection(UUID(int=0))
                headers = _headers(connection)
                calendar_id = quote(connection.calendar_id, safe="")
                _admit_google_request(connection)
                response = _google_request(
                    "GET", f"{GOOGLE_CALENDAR_API}/users/me/calendarList/{calendar_id}",
                    failure_message="Unable to verify platform calendar permissions.",
                    headers=headers, timeout=20,
                )
                if not response.ok:
                    raise GoogleCalendarError("The SHVYA account cannot access the configured calendar. Add that calendar to the dedicated account.")
                entry = _response_json(response, failure_message="Invalid platform calendar access response.")
                if entry.get("accessRole") not in {"owner", "writer"}:
                    raise GoogleCalendarError("The SHVYA account needs write access to its configured calendar.")
                _admit_google_request(connection)
                response = _google_request(
                    "GET", f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}",
                    failure_message="Unable to verify Google Meet support.", headers=headers, timeout=20,
                )
                if not response.ok:
                    raise GoogleCalendarError("Unable to read the configured SHVYA calendar.")
                calendar = _response_json(response, failure_message="Invalid platform calendar response.")
                allowed = (calendar.get("conferenceProperties") or {}).get("allowedConferenceSolutionTypes") or []
                if "hangoutsMeet" not in allowed:
                    raise GoogleCalendarError("The configured Google calendar does not advertise Google Meet support.")
                result.update(live_checked=True, owner_verified=True, calendar_writable=True, google_meet_supported=True)
            except GoogleCalendarError as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=2))
