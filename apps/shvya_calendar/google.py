import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import CalendarBooking, GoogleCalendarConnection


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = (
    "openid email "
    "https://www.googleapis.com/auth/calendar"
)


class GoogleCalendarError(RuntimeError):
    pass


def _google_request(method, url, *, failure_message, **kwargs):
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise GoogleCalendarError(failure_message) from exc


def _response_json(response, *, failure_message):
    try:
        return response.json()
    except ValueError as exc:
        raise GoogleCalendarError(failure_message) from exc


def google_is_configured():
    return bool(
        str(getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") or "").strip()
        and str(
            getattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", "") or ""
        ).strip()
    )


def build_authorize_url(*, redirect_uri, state):
    if not google_is_configured():
        raise GoogleCalendarError("Google Calendar OAuth is not configured.")
    params = {
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return requests.Request("GET", GOOGLE_AUTHORIZE_URL, params=params).prepare().url


def exchange_code(*, code, redirect_uri):
    response = _google_request(
        "POST",
        GOOGLE_TOKEN_URL,
        failure_message="Google Calendar connection is temporarily unavailable.",
        data={
            "code": code,
            "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
            "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if not response.ok:
        raise GoogleCalendarError(
            f"Google token exchange failed ({response.status_code})."
        )
    return _response_json(
        response,
        failure_message="Google returned an invalid token response.",
    )


def fetch_userinfo(access_token):
    response = _google_request(
        "GET",
        GOOGLE_USERINFO_URL,
        failure_message="Unable to read the connected Google account right now.",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not response.ok:
        raise GoogleCalendarError("Unable to read the connected Google account.")
    return _response_json(
        response,
        failure_message="Google returned an invalid account response.",
    )


def save_connection(*, organization, user, token_payload, userinfo):
    connection, _created = GoogleCalendarConnection.objects.get_or_create(
        user=user,
        defaults={"organization": organization},
    )
    if connection.organization_id != organization.id:
        raise ValidationError("Google Calendar connection belongs to another organization.")

    connection.email = str(userinfo.get("email") or user.email)
    connection.access_token = token_payload.get("access_token") or ""
    refresh_token = token_payload.get("refresh_token")
    if refresh_token:
        connection.refresh_token = refresh_token
    try:
        expires_in = int(token_payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    connection.token_expires_at = timezone.now() + timedelta(
        seconds=max(60, expires_in - 60)
    )
    connection.scope = str(token_payload.get("scope") or GOOGLE_SCOPES)
    connection.is_active = True
    connection.last_error = ""
    connection.full_clean()
    connection.save()
    return connection


def _refresh_access_token(connection):
    if (
        connection.access_token
        and connection.token_expires_at
        and connection.token_expires_at > timezone.now()
    ):
        return connection.access_token

    refresh_token = connection.refresh_token
    if not refresh_token:
        raise GoogleCalendarError("Reconnect Google Calendar to renew access.")

    response = _google_request(
        "POST",
        GOOGLE_TOKEN_URL,
        failure_message="Google Calendar token refresh is temporarily unavailable.",
        data={
            "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
            "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if not response.ok:
        connection.last_error = "Google access token refresh failed."
        connection.save(update_fields=["last_error", "updated_at"])
        raise GoogleCalendarError(connection.last_error)

    payload = _response_json(
        response,
        failure_message="Google returned an invalid token refresh response.",
    )
    connection.access_token = payload.get("access_token") or ""
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    connection.token_expires_at = timezone.now() + timedelta(
        seconds=max(60, expires_in - 60)
    )
    connection.last_error = ""
    connection.save(
        update_fields=[
            "access_token_ciphertext",
            "token_expires_at",
            "last_error",
            "updated_at",
        ]
    )
    return connection.access_token


def _headers(connection):
    token = _refresh_access_token(connection)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def connection_for_page(page):
    if not page.host_id:
        return None
    return (
        GoogleCalendarConnection.objects
        .filter(
            user_id=page.host_id,
            organization=page.organization,
            is_active=True,
        )
        .first()
    )


def free_busy(*, page, time_min, time_max):
    connection = connection_for_page(page)
    if connection is None:
        return []

    response = _google_request(
        "POST",
        f"{GOOGLE_CALENDAR_API}/freeBusy",
        failure_message="Live Google Calendar availability is temporarily unavailable.",
        headers=_headers(connection),
        json={
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "items": [{"id": connection.calendar_id or "primary"}],
        },
        timeout=20,
    )
    if not response.ok:
        connection.last_error = (
            f"Google free/busy check failed ({response.status_code})."
        )
        connection.save(update_fields=["last_error", "updated_at"])
        raise GoogleCalendarError(connection.last_error)

    payload = _response_json(
        response,
        failure_message="Google returned an invalid free/busy response.",
    )
    calendar = payload.get("calendars", {}).get(
        connection.calendar_id or "primary",
        {},
    )
    result = []
    for item in calendar.get("busy", []):
        try:
            start = datetime.fromisoformat(
                str(item["start"]).replace("Z", "+00:00")
            )
            end = datetime.fromisoformat(
                str(item["end"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            continue
        result.append((start, end))
    return result


def _conference_details(payload):
    meeting_link = str(payload.get("hangoutLink") or "")
    conference_id = ""
    conference = payload.get("conferenceData") or {}
    conference_id = str((conference.get("conferenceId") or ""))
    if not meeting_link:
        for entry in conference.get("entryPoints") or []:
            if entry.get("entryPointType") == "video":
                meeting_link = str(entry.get("uri") or "")
                break
    return meeting_link, conference_id


def create_booking_event(booking):
    page = booking.page
    connection = connection_for_page(page)
    if connection is None:
        booking.calendar_sync_status = CalendarBooking.SyncStatus.NOT_CONNECTED
        booking.calendar_sync_error = ""
        booking.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
        return booking

    lead = booking.lead
    event = {
        "summary": page.session_title or page.name,
        "description": page.session_description or page.intro_description,
        "start": {
            "dateTime": booking.start_at.isoformat(),
            "timeZone": booking.timezone,
        },
        "end": {
            "dateTime": booking.end_at.isoformat(),
            "timeZone": booking.timezone,
        },
    }
    if page.invite_lead_to_event and lead.email:
        event["attendees"] = [{"email": lead.email, "displayName": lead.name}]

    params = {"sendUpdates": "all" if event.get("attendees") else "none"}
    if page.meeting_location == page.MeetingLocation.GOOGLE_MEET:
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = "1"

    calendar_id = connection.calendar_id or "primary"
    response = _google_request(
        "POST",
        f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events",
        failure_message="Google Calendar event creation is temporarily unavailable.",
        headers=_headers(connection),
        params=params,
        json=event,
        timeout=25,
    )
    if not response.ok:
        message = f"Google Calendar event creation failed ({response.status_code})."
        booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
        booking.calendar_sync_error = message
        booking.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
        return booking

    payload = _response_json(
        response,
        failure_message="Google returned an invalid Calendar event response.",
    )
    meeting_link, conference_id = _conference_details(payload)
    if page.meeting_location == page.MeetingLocation.CUSTOM:
        meeting_link = page.custom_meeting_link

    booking.google_calendar_id = calendar_id
    booking.google_event_id = str(payload.get("id") or "")
    booking.google_event_url = str(payload.get("htmlLink") or "")
    booking.google_conference_id = conference_id
    booking.meeting_link = meeting_link
    conference_pending = (
        page.meeting_location == page.MeetingLocation.GOOGLE_MEET
        and not meeting_link
    )
    booking.calendar_sync_status = (
        CalendarBooking.SyncStatus.PENDING
        if conference_pending
        else CalendarBooking.SyncStatus.SYNCED
    )
    booking.calendar_sync_error = ""
    booking.save(
        update_fields=[
            "google_calendar_id",
            "google_event_id",
            "google_event_url",
            "google_conference_id",
            "meeting_link",
            "calendar_sync_status",
            "calendar_sync_error",
            "updated_at",
        ]
    )
    if conference_pending:
        try:
            from .tasks import refresh_booking_conference

            refresh_booking_conference.apply_async(
                args=[str(booking.id)],
                countdown=3,
            )
        except Exception:
            # Recovery Beat also scans pending Google Meet bookings, so a
            # temporary broker outage cannot permanently lose conference sync.
            pass
    return booking


def refresh_booking_event_details(booking):
    """Refresh Google-owned event/conference details for an existing booking."""
    if not booking.google_event_id:
        return booking

    connection = connection_for_page(booking.page)
    if connection is None:
        raise GoogleCalendarError("Reconnect Google Calendar to finish meeting sync.")

    calendar_id = booking.google_calendar_id or connection.calendar_id or "primary"
    response = _google_request(
        "GET",
        (
            f"{GOOGLE_CALENDAR_API}/calendars/"
            f"{quote(calendar_id, safe='')}/events/"
            f"{quote(booking.google_event_id, safe='')}"
        ),
        failure_message="Google Calendar event refresh is temporarily unavailable.",
        headers=_headers(connection),
        timeout=20,
    )
    if not response.ok:
        raise GoogleCalendarError(
            f"Google Calendar event refresh failed ({response.status_code})."
        )

    event = _response_json(
        response,
        failure_message="Google returned an invalid Calendar event response.",
    )
    meeting_link, conference_id = _conference_details(event)
    if booking.page.meeting_location == booking.page.MeetingLocation.CUSTOM:
        meeting_link = booking.page.custom_meeting_link

    booking.google_event_url = str(
        event.get("htmlLink") or booking.google_event_url
    )
    booking.meeting_link = meeting_link or booking.meeting_link
    booking.google_conference_id = (
        conference_id or booking.google_conference_id
    )
    still_pending = (
        booking.page.meeting_location
        == booking.page.MeetingLocation.GOOGLE_MEET
        and not booking.meeting_link
    )
    booking.calendar_sync_status = (
        CalendarBooking.SyncStatus.PENDING
        if still_pending
        else CalendarBooking.SyncStatus.SYNCED
    )
    booking.calendar_sync_error = ""
    booking.save(
        update_fields=[
            "google_event_url",
            "meeting_link",
            "google_conference_id",
            "calendar_sync_status",
            "calendar_sync_error",
            "updated_at",
        ]
    )
    return booking


def update_booking_event(booking):
    if not booking.google_event_id:
        return create_booking_event(booking)

    connection = connection_for_page(booking.page)
    if connection is None:
        booking.calendar_sync_status = CalendarBooking.SyncStatus.NOT_CONNECTED
        booking.calendar_sync_error = ""
        booking.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
        return booking

    calendar_id = booking.google_calendar_id or connection.calendar_id or "primary"
    payload = {
        "start": {
            "dateTime": booking.start_at.isoformat(),
            "timeZone": booking.timezone,
        },
        "end": {
            "dateTime": booking.end_at.isoformat(),
            "timeZone": booking.timezone,
        },
    }
    if booking.page.invite_lead_to_event and booking.lead.email:
        payload["attendees"] = [
            {"email": booking.lead.email, "displayName": booking.lead.name}
        ]

    response = _google_request(
        "PATCH",
        (
            f"{GOOGLE_CALENDAR_API}/calendars/"
            f"{quote(calendar_id, safe='')}/events/"
            f"{quote(booking.google_event_id, safe='')}"
        ),
        failure_message="Google Calendar event update is temporarily unavailable.",
        headers=_headers(connection),
        params={"sendUpdates": "all"},
        json=payload,
        timeout=25,
    )
    if not response.ok:
        message = (
            f"Google Calendar event update failed ({response.status_code})."
        )
        booking.calendar_sync_status = CalendarBooking.SyncStatus.FAILED
        booking.calendar_sync_error = message
        booking.save(
            update_fields=[
                "calendar_sync_status",
                "calendar_sync_error",
                "updated_at",
            ]
        )
        return booking

    event = _response_json(
        response,
        failure_message="Google returned an invalid Calendar update response.",
    )
    meeting_link, conference_id = _conference_details(event)
    booking.google_event_url = str(event.get("htmlLink") or booking.google_event_url)
    booking.meeting_link = meeting_link or booking.meeting_link
    booking.google_conference_id = conference_id or booking.google_conference_id
    booking.calendar_sync_status = CalendarBooking.SyncStatus.SYNCED
    booking.calendar_sync_error = ""
    booking.save(
        update_fields=[
            "google_event_url",
            "meeting_link",
            "google_conference_id",
            "calendar_sync_status",
            "calendar_sync_error",
            "updated_at",
        ]
    )
    return booking


def cancel_booking_event(booking):
    if not booking.google_event_id:
        return
    connection = connection_for_page(booking.page)
    if connection is None:
        return
    calendar_id = booking.google_calendar_id or connection.calendar_id or "primary"
    response = _google_request(
        "DELETE",
        (
            f"{GOOGLE_CALENDAR_API}/calendars/"
            f"{quote(calendar_id, safe='')}/events/"
            f"{quote(booking.google_event_id, safe='')}"
        ),
        failure_message="Google Calendar cancellation is temporarily unavailable.",
        headers=_headers(connection),
        params={"sendUpdates": "all"},
        timeout=20,
    )
    if response.status_code not in {200, 204, 404, 410}:
        raise GoogleCalendarError(
            f"Google Calendar event cancellation failed ({response.status_code})."
        )
