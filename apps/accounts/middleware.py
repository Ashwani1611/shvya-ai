from django.contrib.auth import get_user_model
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
)

from .session_utils import (
    get_session_store,
)


User = get_user_model()


class SHVYAAreaAuthenticationMiddleware:
    """
    Loads authentication from the appropriate SHVYA session.

    /admin/       -> Django's standard session
    /superadmin/  -> SHVYA Superadmin session
    /dashboard/  -> SHVYA CRM session

    This middleware is intentionally placed AFTER Django's normal
    AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        area = self._get_area(request)

        request.shvya_session_area = area

        if area in ("superadmin", "dashboard"):
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
        # ----------------------------------------------------

        if session_hash:

            if session_hash != user.get_session_auth_hash():

                self._clear_invalid_session(
                    session,
                )

                return

        # ----------------------------------------------------
        # Area-specific authorization
        # ----------------------------------------------------

        if area == "superadmin":

            if not user.is_authenticated:
                return

            if not user.is_superuser:
                self._clear_invalid_session(
                    session,
                )

                return

        elif area == "dashboard":

            if not user.is_authenticated:
                return

            if user.is_superuser:
                self._clear_invalid_session(
                    session,
                )

                return

            if not user.is_active:
                self._clear_invalid_session(
                    session,
                )

                return

        request.user = user

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