from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    get_user_model,
)
from django.contrib.auth.models import AnonymousUser

from .session_utils import get_session_store

User = get_user_model()


class SHVYAAreaAuthenticationMiddleware:
    """
    Loads authentication from the appropriate SHVYA session.

    /admin/       -> Django's standard session (untouched here)
    /superadmin/  -> SHVYA Superadmin session
    /dashboard/   -> SHVYA CRM session

    This middleware is intentionally placed AFTER Django's normal
    AuthenticationMiddleware.

    SECURITY (fixed):
    Django's AuthenticationMiddleware already populated
    request.user from the DEFAULT `sessionid` cookie, regardless
    of which path is being requested. Previously, if the dedicated
    /superadmin/ or /dashboard/ session cookie was missing or had
    no valid auth, this middleware just returned early -- leaving
    request.user as whatever the default session cookie produced.

    That means a session authenticated only through /admin/ (or
    through no dedicated area session at all) could leak straight
    into /superadmin/ or /dashboard/ views, defeating the entire
    point of separating these sessions.

    Fix: for these two areas we ALWAYS reset request.user to
    AnonymousUser first, and only replace it if the dedicated area
    session proves out a valid, authorized user for that area.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        area = self._get_area(request)

        request.shvya_session_area = area

        if area in ("superadmin", "dashboard"):
            # Never trust the default-session identity for these
            # areas. Reset first, then re-populate only on success.
            request.user = AnonymousUser()
            request.crm_user = None

            self._load_area_user(
                request,
                area,
            )

        response = self.get_response(request)

        return response

    # ========================================================
    # AREA DETECTION
    # ========================================================

    @staticmethod
    def _get_area(request):

        path = request.path

        if path.startswith("/superadmin/"):
            return "superadmin"

        if path.startswith("/dashboard/"):
            return "dashboard"

        if path.startswith("/admin/"):
            return "admin"

        return None

    # ========================================================
    # LOAD AREA USER
    # ========================================================

    def _load_area_user(
        self,
        request,
        area,
    ):

        session = get_session_store(
            request,
            area,
        )

        request.shvya_session = session

        user_id = session.get(
            SESSION_KEY,
        )

        backend = session.get(
            BACKEND_SESSION_KEY,
        )

        session_hash = session.get(
            HASH_SESSION_KEY,
        )

        # request.user is already AnonymousUser (reset in __call__),
        # so bailing out here simply leaves the request anonymous --
        # it no longer inherits the default session's identity.
        if not user_id or not backend:
            return

        try:

            user = User.objects.get(
                pk=user_id,
            )

        except User.DoesNotExist:

            self._clear_invalid_session(
                session,
            )

            return

        # ----------------------------------------------------
        # Verify session authentication hash
        #
        # A session with NO stored hash at all used to skip this
        # check entirely and fall through as if it were valid.
        # Treat a missing hash the same as a mismatched one.
        # ----------------------------------------------------

        if not session_hash or session_hash != user.get_session_auth_hash():

            self._clear_invalid_session(
                session,
            )

            return

        # ----------------------------------------------------
        # Area-specific authorization
        # ----------------------------------------------------

        if area == "superadmin":

            if not user.is_active:
                self._clear_invalid_session(session)
                return

            if not user.is_superuser:
                self._clear_invalid_session(
                    session,
                )

                return

        elif area == "dashboard":

            if not user.is_active:
                self._clear_invalid_session(
                    session,
                )

                return

            if user.is_superuser:
                self._clear_invalid_session(
                    session,
                )

                return

        request.user = user

        if area == "dashboard":
            request.crm_user = user

    # ========================================================
    # INVALID SESSION
    # ========================================================

    @staticmethod
    def _clear_invalid_session(session):

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