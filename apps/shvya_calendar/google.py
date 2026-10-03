from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import CalendarBooking, CalendarPage, GoogleCalendarConnection
from .platform_google import (
    PlatformGoogleConnection, check_platform_event, connection_for_booking,
    is_platform_booking, platform_access_token, platform_event_id,
)

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/calendar"
ACTIVE = (CalendarBooking.Status.SCHEDULED, CalendarBooking.Status.RESCHEDULED)


class GoogleCalendarError(RuntimeError):
    def __init__(self, message, *, status_code=None, transient=False, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.transient = bool(transient)
        self.retry_after = retry_after


def _retry_after_seconds(response):
    raw = str((getattr(response, "headers", {}) or {}).get("Retry-After") or "").strip()
    try:
        return max(1, min(int(float(raw)), 900)) if raw else None
    except (TypeError, ValueError):
        return None


def _google_error(response, message):
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 429:
        from apps.core.observability import increment
        increment("provider.throttled", labels={"provider": "google_calendar"})
    return GoogleCalendarError(message, status_code=status or None,
                               transient=status in {408, 425, 429} or status >= 500,
                               retry_after=_retry_after_seconds(response))


def _admit_google_request(connection):
    from apps.core.fairness import admit_provider_start
    allowed, retry_after, _scope = admit_provider_start(
        provider="google_calendar", account_id=connection.organization_id,
        account_limit=settings.GOOGLE_CALENDAR_ORGANIZATION_REQUESTS_PER_MINUTE,
        global_limit=settings.GOOGLE_CALENDAR_GLOBAL_REQUESTS_PER_MINUTE,
    )
    if not allowed:
        raise GoogleCalendarError("Google Calendar is temporarily busy.",
                                  status_code=429, transient=True, retry_after=retry_after)


def _google_request(method, url, *, failure_message, **kwargs):
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise GoogleCalendarError(failure_message, transient=True) from exc


def _response_json(response, *, failure_message):
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleCalendarError(failure_message) from exc
    if not isinstance(payload, dict):
        raise GoogleCalendarError(failure_message)
    return payload


def google_is_configured():
    return bool(str(getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") or "").strip()
                and str(getattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", "") or "").strip())


def build_authorize_url(*, redirect_uri, state):
    if not google_is_configured():
        raise GoogleCalendarError("Google Calendar OAuth is not configured.")
    params = {"client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
              "redirect_uri": redirect_uri, "response_type": "code",
              "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state}
    return requests.Request("GET", GOOGLE_AUTHORIZE_URL, params=params).prepare().url


def exchange_code(*, code, redirect_uri):
    response = _google_request(
        "POST", GOOGLE_TOKEN_URL,
        failure_message="Google Calendar connection is temporarily unavailable.",
        data={"code": code, "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
              "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
              "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=20,
    )
    if not response.ok:
        raise GoogleCalendarError(f"Google token exchange failed ({response.status_code}).")
    return _response_json(response, failure_message="Google returned an invalid token response.")


def fetch_userinfo(access_token):
    response = _google_request(
        "GET", GOOGLE_USERINFO_URL,
        failure_message="Unable to read the connected Google account right now.",
        headers={"Authorization": f"Bearer {access_token}"}, timeout=20,
    )
    if not response.ok:
        raise GoogleCalendarError("Unable to read the connected Google account.")
    return _response_json(response, failure_message="Google returned an invalid account response.")


def save_connection(*, organization, user, token_payload, userinfo):
    connection, _created = GoogleCalendarConnection.objects.get_or_create(
        user=user, defaults={"organization": organization},
    )
    if connection.organization_id != organization.id:
        raise ValidationError("Google Calendar connection belongs to another organization.")
    connection.email = str(userinfo.get("email") or user.email)
    connection.access_token = token_payload.get("access_token") or ""
    if token_payload.get("refresh_token"):
        connection.refresh_token = token_payload["refresh_token"]
    try:
        expires_in = int(token_payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    connection.token_expires_at = timezone.now() + timedelta(seconds=max(60, expires_in - 60))
    connection.scope = str(token_payload.get("scope") or GOOGLE_SCOPES)
    connection.is_active = True
    connection.last_error = ""
    connection.full_clean()
    connection.save()
    from .attribute_sync import enqueue_booking_sync
    pending = CalendarBooking.objects.filter(
        organization=organization, host=user, page__host=user, status__in=ACTIVE,
        start_at__gt=timezone.now(), calendar_sync_status=CalendarBooking.SyncStatus.NOT_CONNECTED,
    )
    booking_ids = list(pending.values_list("pk", flat=True))
    pending.update(calendar_sync_status=CalendarBooking.SyncStatus.PENDING)
    for booking_id in booking_ids:
        transaction.on_commit(lambda pk=booking_id: enqueue_booking_sync(pk))
    return connection


def _refresh_access_token(connection):
    if isinstance(connection, PlatformGoogleConnection):
        return platform_access_token(connection)
    if (connection.access_token and connection.token_expires_at
            and connection.token_expires_at > timezone.now()):
        return connection.access_token
    if not connection.refresh_token:
        raise GoogleCalendarError("Reconnect Google Calendar to renew access.")
    _admit_google_request(connection)
    response = _google_request(
        "POST", GOOGLE_TOKEN_URL,
        failure_message="Google Calendar token refresh is temporarily unavailable.",
        data={"client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
              "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
              "refresh_token": connection.refresh_token, "grant_type": "refresh_token"}, timeout=20,
    )
    if not response.ok:
        connection.last_error = "Google access token refresh failed."
        connection.save(update_fields=["last_error", "updated_at"])
        raise _google_error(response, connection.last_error)
    payload = _response_json(response, failure_message="Google returned an invalid token refresh response.")
    connection.access_token = payload.get("access_token") or ""
    if not connection.access_token:
        raise GoogleCalendarError("Google did not return an access token.")
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    connection.token_expires_at = timezone.now() + timedelta(seconds=max(60, expires_in - 60))
    connection.last_error = ""
    connection.save(update_fields=["access_token_ciphertext", "token_expires_at", "last_error", "updated_at"])
    return connection.access_token


def _headers(connection):
    return {"Authorization": f"Bearer {_refresh_access_token(connection)}", "Content-Type": "application/json"}


def connection_for_page(page):
    # Availability uses only this organisation's host, NEVER the shared calendar.
    if not page.host_id:
        return None
    return GoogleCalendarConnection.objects.filter(
        user_id=page.host_id, organization=page.organization, is_active=True,
    ).first()


def free_busy(*, page, time_min, time_max):
    connection = connection_for_page(page)
    if connection is None:
        return []
    _admit_google_request(connection)
    response = _google_request(
        "POST", f"{GOOGLE_CALENDAR_API}/freeBusy",
        failure_message="Live Google Calendar availability is temporarily unavailable.",
        headers=_headers(connection),
        json={"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
              "items": [{"id": connection.calendar_id or "primary"}]}, timeout=20,
    )
    if not response.ok:
        connection.last_error = f"Google free/busy check failed ({response.status_code})."
        connection.save(update_fields=["last_error", "updated_at"])
        raise _google_error(response, connection.last_error)
    payload = _response_json(response, failure_message="Google returned an invalid free/busy response.")
    calendar = payload.get("calendars", {}).get(connection.calendar_id or "primary", {})
    if calendar.get("errors"):
        raise GoogleCalendarError("Google Calendar could not check conflicts. Reconnect the booking host's calendar.")
    result = []
    for item in calendar.get("busy", []):
        try:
            start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        result.append((start, end))
    return result


def _conference_details(payload):
    meeting_link = str(payload.get("hangoutLink") or "")
    conference = payload.get("conferenceData") or {}
    if not isinstance(conference, dict):
        raise GoogleCalendarError("Google returned invalid conference details.")
    if not meeting_link:
        for entry in conference.get("entryPoints") or []:
            if isinstance(entry, dict) and entry.get("entryPointType") == "video":
                meeting_link = str(entry.get("uri") or "")
                break
    return meeting_link, str(conference.get("conferenceId") or "")


def _not_connected(booking):
    booking.calendar_sync_status = CalendarBooking.SyncStatus.NOT_CONNECTED
    booking.calendar_sync_error = "Connect the booking host's Google account or configure SHVYA-managed Google Meet in the backend."
    booking.save(update_fields=["calendar_sync_status", "calendar_sync_error", "updated_at"])
    return booking


def _event_url(booking, connection):
    calendar_id = booking.google_calendar_id or connection.calendar_id or "primary"
    return (f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(booking.google_event_id, safe='')}")


def _send_updates(booking):
    return "all" if booking.page.invite_lead_to_event and booking.lead.email else "none"


def _event_error(booking, response, message):
    booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
    booking.calendar_sync_error = message
    booking.save(update_fields=["calendar_sync_status", "calendar_sync_error", "updated_at"])
    return _google_error(response, message)


def _save_event_details(booking, event, *, touch_updated_at=True):
    check_platform_event(booking, event)
    link, conference_id = _conference_details(event)
    if booking.page.meeting_location == CalendarPage.MeetingLocation.CUSTOM:
        link = booking.page.custom_meeting_link
    booking.google_event_url = str(event.get("htmlLink") or booking.google_event_url)
    booking.meeting_link = link or booking.meeting_link
    booking.google_conference_id = conference_id or booking.google_conference_id
    conference = event.get("conferenceData") or {}
    failed = ((conference.get("createRequest") or {}).get("status") or {}).get("statusCode") == "failure"
    pending = booking.page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET and not booking.meeting_link
    booking.calendar_sync_status = (CalendarBooking.SyncStatus.FAILED if failed
                                    else CalendarBooking.SyncStatus.PENDING if pending
                                    else CalendarBooking.SyncStatus.SYNCED)
    booking.calendar_sync_error = "Google could not create this Meet conference. Check the organiser's Meet permissions." if failed else ""
    fields = ["google_event_url", "meeting_link", "google_conference_id",
              "calendar_sync_status", "calendar_sync_error"]
    if touch_updated_at:
        fields.append("updated_at")
    booking.save(update_fields=fields)
    if failed:
        raise GoogleCalendarError(booking.calendar_sync_error)
    return booking


def create_booking_event(booking, *, recovering=False):
    connection = connection_for_booking(booking)
    if connection is None:
        return _not_connected(booking)
    platform = isinstance(connection, PlatformGoogleConnection)
    proposed_id = platform_event_id(booking) if platform else booking.id.hex
    calendar_id = connection.calendar_id or "primary"
    email = getattr(connection, "email", "")
    if calendar_id == "primary" and isinstance(email, str) and email:
        calendar_id = email
    # Compare-and-set makes the selected provider durable before external HTTP.
    if not booking.google_event_id:
        CalendarBooking.objects.filter(pk=booking.pk, organization_id=booking.organization_id,
                                       google_event_id="", status__in=ACTIVE).update(
            google_event_id=proposed_id, google_calendar_id=calendar_id,
            calendar_sync_status=CalendarBooking.SyncStatus.PENDING,
        )
        booking.refresh_from_db(fields=["google_event_id", "google_calendar_id", "status"])
        if booking.status not in ACTIVE:
            return booking
        connection = connection_for_booking(booking)
        if connection is None:
            return _not_connected(booking)
        platform = isinstance(connection, PlatformGoogleConnection)
    if not booking.google_event_id:
        raise GoogleCalendarError("Unable to reserve the Google booking identity.")
    page = booking.page
    event = {"id": booking.google_event_id, "summary": page.session_title or page.name,
             "description": page.session_description or page.intro_description,
             "start": {"dateTime": booking.start_at.isoformat(), "timeZone": booking.timezone},
             "end": {"dateTime": booking.end_at.isoformat(), "timeZone": booking.timezone}}
    if page.invite_lead_to_event and booking.lead.email:
        event["attendees"] = [{"email": booking.lead.email, "displayName": booking.lead.name}]
    params = {"sendUpdates": _send_updates(booking)}
    if page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET:
        request_id = platform_event_id(booking) if platform else booking.id.hex
        event["conferenceData"] = {"createRequest": {"requestId": request_id,
                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        params["conferenceDataVersion"] = "1"
    if platform:
        event.update(visibility="private", transparency="transparent",
                     guestsCanInviteOthers=False, guestsCanSeeOtherGuests=False,
                     guestsCanModify=False, reminders={"useDefault": False},
                     extendedProperties={"private": {"shvya_organization_id": str(booking.organization_id),
                                                      "shvya_booking_id": str(booking.pk)}})
    _admit_google_request(connection)
    response = _google_request(
        "POST", _event_url(booking, connection).rsplit("/", 1)[0],
        failure_message="Google Calendar event creation is temporarily unavailable.",
        headers=_headers(connection), params=params, json=event, timeout=25,
    )
    if response.status_code == 409:
        if recovering:
            raise GoogleCalendarError("Google is still reconciling this event. Retry shortly.", transient=True)
        return refresh_booking_event_details(booking)
    if not response.ok:
        raise _event_error(booking, response, f"Google Calendar event creation failed ({response.status_code}).")
    payload = _response_json(response, failure_message="Google returned an invalid Calendar event response.")
    if not payload.get("id"):
        raise GoogleCalendarError("Google did not return an event identifier.")
    if platform:
        check_platform_event(booking, payload)
    booking.google_event_id = str(payload["id"])
    booking.save(update_fields=["google_event_id", "updated_at"])
    _save_event_details(booking, payload)
    if booking.calendar_sync_status == CalendarBooking.SyncStatus.PENDING:
        try:
            from .tasks import refresh_booking_conference
            refresh_booking_conference.apply_async(args=[str(booking.id)], countdown=3)
        except Exception:
            # Beat recovers the durable pending state after broker outages.
            pass
    return booking


def refresh_booking_event_details(booking):
    if not booking.google_event_id:
        return booking
    connection = connection_for_booking(booking)
    if connection is None:
        raise GoogleCalendarError("Reconnect Google Calendar to finish meeting sync.")
    _admit_google_request(connection)
    response = _google_request(
        "GET", _event_url(booking, connection),
        failure_message="Google Calendar event refresh is temporarily unavailable.",
        headers=_headers(connection), timeout=20,
    )
    if response.status_code == 404 and (
        is_platform_booking(booking) or booking.google_event_id == booking.id.hex
    ):
        return create_booking_event(booking, recovering=True)
    if not response.ok:
        raise _google_error(response, f"Google Calendar event refresh failed ({response.status_code}).")
    event = _response_json(response, failure_message="Google returned an invalid Calendar event response.")
    return _save_event_details(booking, event)


def update_booking_event(booking):
    if not booking.google_event_id:
        return create_booking_event(booking)
    connection = connection_for_booking(booking)
    if connection is None:
        return _not_connected(booking)
    payload = {"start": {"dateTime": booking.start_at.isoformat(), "timeZone": booking.timezone},
               "end": {"dateTime": booking.end_at.isoformat(), "timeZone": booking.timezone}}
    if booking.page.invite_lead_to_event and booking.lead.email:
        payload["attendees"] = [{"email": booking.lead.email, "displayName": booking.lead.name}]
    _admit_google_request(connection)
    response = _google_request(
        "PATCH", _event_url(booking, connection),
        failure_message="Google Calendar event update is temporarily unavailable.",
        headers=_headers(connection), params={"sendUpdates": _send_updates(booking), "conferenceDataVersion": "1"},
        json=payload, timeout=25,
    )
    if response.status_code == 404 and (
        is_platform_booking(booking) or booking.google_event_id == booking.id.hex
    ):
        return create_booking_event(booking, recovering=True)
    if not response.ok:
        raise _event_error(booking, response, f"Google Calendar event update failed ({response.status_code}).")
    event = _response_json(response, failure_message="Google returned an invalid Calendar update response.")
    return _save_event_details(booking, event)


def cancel_booking_event(booking):
    if not booking.google_event_id:
        return
    connection = connection_for_booking(booking)
    if connection is None:
        raise GoogleCalendarError("Reconnect Google Calendar to cancel the Google event.")
    _admit_google_request(connection)
    response = _google_request(
        "DELETE", _event_url(booking, connection),
        failure_message="Google Calendar cancellation is temporarily unavailable.",
        headers=_headers(connection), params={"sendUpdates": _send_updates(booking)}, timeout=20,
    )
    if response.status_code not in {200, 204, 404, 410}:
        raise _google_error(response, f"Google Calendar event cancellation failed ({response.status_code}).")
