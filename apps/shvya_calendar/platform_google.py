"""Optional SHVYA-owned Google Calendar credentials, never tenant credentials.

Configuration may be supplied as Django settings or server .env variables.
Only a dedicated Google account explicitly authorised by SHVYA is supported.
Access tokens are kept in process memory, not a shared tenant cache.
"""
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic

from decouple import config
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


_token_lock = Lock()
_token_cache = {}


def _setting(name, default=""):
    if hasattr(settings, name):
        return getattr(settings, name)
    return config(name, default=default)


def _text(name, default=""):
    return str(_setting(name, default) or "").strip()


def platform_enabled():
    return _text("GOOGLE_CALENDAR_PLATFORM_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }


def platform_status():
    """Safe configuration diagnostics: never return any credential values."""
    required = (
        "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN",
        "GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL",
    )
    missing = [name for name in required if not _text(name)]
    invalid = []
    email = _text("GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL")
    if email:
        try:
            validate_email(email)
        except ValidationError:
            invalid.append("GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL")
    return {
        "enabled": platform_enabled(),
        "configured": not missing and not invalid,
        "missing": missing,
        "invalid": invalid,
    }


@dataclass(frozen=True, repr=False)
class PlatformGoogleConnection:
    organization_id: object
    email: str
    calendar_id: str


def platform_connection(organization_id):
    if not platform_enabled():
        return None
    status = platform_status()
    if not status["configured"]:
        from .google import GoogleCalendarError
        names = ", ".join(status["missing"] + status["invalid"])
        raise GoogleCalendarError(
            "SHVYA-managed Google Meet needs backend configuration: " + names
        )
    email = _text("GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL").lower()
    calendar_id = _text("GOOGLE_CALENDAR_PLATFORM_CALENDAR_ID", email)
    # Pin a concrete calendar, never the account-relative 'primary' alias.
    if not calendar_id or calendar_id == "primary":
        calendar_id = email
    return PlatformGoogleConnection(organization_id, email, calendar_id)


def platform_event_id(booking):
    # Google event IDs accept base32hex (0-9, a-v). A UUID is 32 hex digits;
    # this 34-character namespace unambiguously identifies SHVYA-owned events.
    return "5a" + booking.id.hex


def is_platform_booking(booking):
    return booking.google_event_id == platform_event_id(booking)


def platform_access_token(connection):
    from .google import (
        GOOGLE_TOKEN_URL, GOOGLE_USERINFO_URL, GoogleCalendarError,
        _admit_google_request, _google_error, _google_request, _response_json,
    )
    refresh_token = _text("GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN")
    client_id = _text("GOOGLE_CALENDAR_CLIENT_ID")
    client_secret = _text("GOOGLE_CALENDAR_CLIENT_SECRET")
    fingerprint = sha256(
        "\0".join((client_id, client_secret, refresh_token, connection.email)).encode()
    ).hexdigest()
    with _token_lock:
        if (
            _token_cache.get("key") == fingerprint
            and _token_cache.get("expires_at", 0) > monotonic()
        ):
            return _token_cache["access_token"]
        _admit_google_request(connection)
        response = _google_request(
            "POST", GOOGLE_TOKEN_URL,
            failure_message="SHVYA Google authorisation is temporarily unavailable.",
            data={"client_id": client_id, "client_secret": client_secret,
                  "refresh_token": refresh_token, "grant_type": "refresh_token"},
            timeout=20,
        )
        if not response.ok:
            raise _google_error(
                response,
                "SHVYA Google authorisation failed. Reauthorise the dedicated "
                "platform account and update its backend refresh token.",
            )
        payload = _response_json(response, failure_message="Invalid SHVYA Google token response.")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GoogleCalendarError("Google did not return a platform access token.")
        # Prevent accidental credential rotation to a different Google owner.
        _admit_google_request(connection)
        identity_response = _google_request(
            "GET", GOOGLE_USERINFO_URL,
            failure_message="Unable to verify the SHVYA Google account.",
            headers={"Authorization": "Bearer " + token}, timeout=20,
        )
        if not identity_response.ok:
            raise _google_error(identity_response, "Unable to verify the SHVYA Google account. Authorise openid email and Calendar access.")
        identity = _response_json(identity_response, failure_message="Invalid SHVYA Google account response.")
        if (
            not isinstance(identity, dict)
            or str(identity.get("email", "")).strip().lower() != connection.email
            or identity.get("verified_email") is not True
        ):
            raise GoogleCalendarError("The authorised Google account does not match GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL.")
        try:
            lifetime = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        _token_cache.clear()
        _token_cache.update(key=fingerprint, access_token=token,
                            expires_at=monotonic() + max(0, min(lifetime, 3600) - 60))
        return token


def connection_for_booking(booking):
    """Keep provider ownership sticky, including when a host connects later."""
    from .google import GoogleCalendarError, connection_for_page
    if (
        booking.page.organization_id != booking.organization_id
        or booking.lead.organization_id != booking.organization_id
    ):
        raise GoogleCalendarError("Booking, lead and calendar must belong to the same organisation.")
    if is_platform_booking(booking):
        connection = platform_connection(booking.organization_id)
        if connection is None:
            raise GoogleCalendarError("Re-enable SHVYA-managed Google Meet to sync this platform-owned booking.")
        if booking.google_calendar_id != connection.calendar_id:
            raise GoogleCalendarError("This booking belongs to the previous SHVYA Google calendar. Restore that calendar configuration before syncing it.")
        return connection
    host_connection = connection_for_page(booking.page)
    # Never move an existing organisation-owned event to platform credentials.
    if host_connection is not None or booking.google_event_id:
        return host_connection
    if booking.page.meeting_location != booking.page.MeetingLocation.GOOGLE_MEET:
        return None
    return platform_connection(booking.organization_id)


def check_platform_event(booking, event):
    """Do not attach a link from an event owned by a different tenant/booking."""
    if not is_platform_booking(booking):
        return
    from .google import GoogleCalendarError
    private = (event.get("extendedProperties") or {}).get("private") or {}
    if (
        event.get("id") != platform_event_id(booking)
        or private.get("shvya_organization_id") != str(booking.organization_id)
        or private.get("shvya_booking_id") != str(booking.pk)
    ):
        raise GoogleCalendarError("Google event ownership did not match this SHVYA booking.")
