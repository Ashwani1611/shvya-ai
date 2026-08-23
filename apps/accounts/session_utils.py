from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
)
from django.contrib.sessions.backends.db import SessionStore


# ============================================================
# SHVYA SESSION CONFIGURATION
# ============================================================

SHVYA_SESSION_COOKIES = {
    "admin": "sessionid",
    "superadmin": "shvya_superadmin_sessionid",
    "dashboard": "shvya_crm_sessionid",
}


# ============================================================
# SESSION HELPERS
# ============================================================


def get_session_cookie_name(area):
    """
    Return the session cookie used by a SHVYA web area.

    Areas:

        admin       -> Django technical administration
        superadmin  -> SHVYA business administration
        dashboard   -> CRM workspace
    """

    try:
        return SHVYA_SESSION_COOKIES[area]
    except KeyError:
        raise ValueError(
            f"Unknown SHVYA session area: {area}"
        )


def get_session_key(request, area):
    """
    Get the session key stored in the cookie belonging to
    the requested SHVYA area.
    """

    cookie_name = get_session_cookie_name(area)

    return request.COOKIES.get(cookie_name)


def get_session_store(request, area):
    """
    Load the database-backed Django session belonging to
    the requested SHVYA area.

    If no session cookie exists, return a new empty session.
    """

    session_key = get_session_key(
        request,
        area,
    )

    if session_key:
        session = SessionStore(
            session_key=session_key,
        )

        try:
            session.load()
            return session

        except Exception:
            # Invalid / expired / corrupted session.
            # Start a fresh session instead.
            pass

    return SessionStore()


def save_session_cookie(
    request,
    response,
    session,
    area,
):
    """
    Persist the selected session and attach its cookie to
    the response.

    This deliberately does not modify Django's global
    SESSION_COOKIE_NAME setting.
    """

    if session.session_key is None:
        session.create()

    else:
        session.save(
            must_create=False,
        )

    cookie_name = get_session_cookie_name(
        area,
    )

    max_age = getattr(
        settings,
        "SESSION_COOKIE_AGE",
        1209600,
    )

    response.set_cookie(
        cookie_name,
        session.session_key,
        max_age=max_age,
        expires=None,
        domain=getattr(
            settings,
            "SESSION_COOKIE_DOMAIN",
            None,
        ),
        path=getattr(
            settings,
            "SESSION_COOKIE_PATH",
            "/",
        ),
        secure=getattr(
            settings,
            "SESSION_COOKIE_SECURE",
            False,
        ),
        httponly=getattr(
            settings,
            "SESSION_COOKIE_HTTPONLY",
            True,
        ),
        samesite=getattr(
            settings,
            "SESSION_COOKIE_SAMESITE",
            "Lax",
        ),
    )

    return response


def delete_session_cookie(
    response,
    area,
):
    """
    Delete the session cookie for one SHVYA area.
    """

    cookie_name = get_session_cookie_name(
        area,
    )

    response.delete_cookie(
        cookie_name,
        path=getattr(
            settings,
            "SESSION_COOKIE_PATH",
            "/",
        ),
        domain=getattr(
            settings,
            "SESSION_COOKIE_DOMAIN",
            None,
        ),
        samesite=getattr(
            settings,
            "SESSION_COOKIE_SAMESITE",
            "Lax",
        ),
    )

    return response


# ============================================================
# AUTHENTICATION STATE
# ============================================================


def set_authenticated_user(
    session,
    user,
):
    """
    Store Django authentication state inside the selected
    session.

    This mirrors the values Django's login() function stores,
    but does so in the independently selected session.
    """

    session[SESSION_KEY] = str(
        user._meta.pk.value_to_string(user)
    )

    session[BACKEND_SESSION_KEY] = (
        "django.contrib.auth.backends.ModelBackend"
    )

    session[HASH_SESSION_KEY] = (
        user.get_session_auth_hash()
    )

    session.modified = True


def clear_authenticated_user(session):
    """
    Remove Django authentication state from a selected
    session without touching any other SHVYA session.
    """

    session.pop(
        SESSION_KEY,
        None,
    )

    session.pop(
        BACKEND_SESSION_KEY,
        None,
    )

    session.pop(
        HASH_SESSION_KEY,
        None,
    )

    session.modified = True