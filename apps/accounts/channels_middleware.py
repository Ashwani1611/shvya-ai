"""
ASGI (WebSocket) authentication middleware for the CRM dashboard
area.

WHY THIS EXISTS -- don't replace it with Channels' built-in
`channels.auth.AuthMiddlewareStack`:

SHVYA does not use Django's default `sessionid` cookie for CRM
dashboard users. `SHVYAAreaAuthenticationMiddleware`
(apps/accounts/middleware.py) authenticates /dashboard/ HTTP
requests against a dedicated `shvya_crm_sessionid` cookie instead
(see apps/accounts/session_utils.py -- SHVYA_SESSION_COOKIES).
Channels' built-in auth middleware only ever looks at the default
`sessionid` cookie, so it would leave every WebSocket connection
unauthenticated for this app. This middleware mirrors the exact
session-validation logic from SHVYAAreaAuthenticationMiddleware's
dashboard branch, applied to the ASGI `scope` instead of an HTTP
`request`.

Sets `scope["crm_user"]` to the authenticated CRM user, or `None`
if there's no valid dashboard session. Consumers must check for
`None` and close the connection themselves -- this middleware
does not reject connections on its own, since a websocket route
outside /ws/whatsapp/ might one day want different rules.
"""

from urllib.parse import unquote

from channels.db import database_sync_to_async
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    get_user_model,
)
from django.contrib.sessions.backends.db import SessionStore

CRM_SESSION_COOKIE_NAME = "shvya_crm_sessionid"


def _parse_cookies(scope):
    """
    ASGI scope headers are a list of (bytes, bytes) pairs, not a
    dict -- unlike Django's request.COOKIES, nothing parses this
    for us here.
    """

    headers = dict(scope.get("headers") or [])

    raw = headers.get(b"cookie", b"").decode("latin-1")

    cookies = {}

    for pair in raw.split(";"):

        pair = pair.strip()

        if not pair or "=" not in pair:
            continue

        key, _, value = pair.partition("=")

        cookies[key.strip()] = unquote(value.strip())

    return cookies


@database_sync_to_async
def _get_crm_user(session_key):
    """
    Same validation as SHVYAAreaAuthenticationMiddleware's
    dashboard branch: session must exist, resolve to an active,
    non-superuser account, and its stored auth hash must still
    match the user (so a changed/reset password invalidates
    existing sessions here too).
    """

    User = get_user_model()

    if not session_key:
        return None

    session = SessionStore(session_key=session_key)

    try:
        session.load()

    except Exception:
        return None

    user_id = session.get(SESSION_KEY)
    backend = session.get(BACKEND_SESSION_KEY)
    session_hash = session.get(HASH_SESSION_KEY)

    if not user_id or not backend:
        return None

    try:
        user = User.objects.select_related(
            "organization",
        ).get(
            pk=user_id,
        )

    except User.DoesNotExist:
        return None

    if not session_hash or session_hash != user.get_session_auth_hash():
        return None

    if not user.is_active or user.is_superuser:
        return None

    return user


class CRMSessionAuthMiddleware:
    """
    ASGI middleware -- wraps the next application in the stack,
    populating scope["crm_user"] before it runs.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):

        cookies = _parse_cookies(scope)

        session_key = cookies.get(CRM_SESSION_COOKIE_NAME)

        scope["crm_user"] = await _get_crm_user(session_key)

        return await self.app(scope, receive, send)