import logging

from django.contrib.auth import authenticate
from django.shortcuts import redirect, render
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.session_utils import (
    get_session_store,
    save_session_cookie,
    set_authenticated_user,
)

logger = logging.getLogger(__name__)

from apps.crm.constants import CRM_SESSION_AREA
from apps.organizations.models import APIKey


class APIKeyPrincipal:

    is_authenticated = True
    is_anonymous = False

    def __init__(self, api_key):
        self.api_key = api_key
        self.organization = api_key.organization

    def __str__(self):
        return f"SHVYA API Key: {self.api_key.name}"


class SHVYAAPIKeyAuthentication(
    BaseAuthentication
):

    keyword = "X-SHVYA-API-KEY"

    def authenticate(self, request):

        raw_key = request.headers.get(
            self.keyword
        )

        if not raw_key:
            raise AuthenticationFailed(
                "X-SHVYA-API-KEY header is required."
            )

        prefix = raw_key[:16]

        api_key = (
            APIKey.objects
            .select_related("organization")
            .filter(
                key_prefix=prefix,
                is_active=True,
            )
            .first()
        )

        if api_key is None:
            raise AuthenticationFailed(
                "Invalid SHVYA API key."
            )

        if (
            api_key.expires_at
            and api_key.expires_at <= timezone.now()
        ):
            raise AuthenticationFailed(
                "SHVYA API key has expired."
            )

        if not api_key.verify(raw_key):
            raise AuthenticationFailed(
                "Invalid SHVYA API key."
            )

        api_key.last_used_at = timezone.now()

        api_key.save(
            update_fields=[
                "last_used_at",
            ]
        )

        return (
            APIKeyPrincipal(api_key),
            api_key,
        )

    def authenticate_header(
        self,
        request,
    ):
        return self.keyword

def get_crm_session(request):
    """
    Return the dedicated SHVYA CRM session.

    The CRM uses its own cookie:

        shvya_crm_sessionid

    This keeps the CRM session independent from:

        - Django Admin
        - Superadmin
        - Other SHVYA areas
    """

    return get_session_store(
        request,
        CRM_SESSION_AREA,
    )


def crm_session_is_authenticated(request):
    """
    Determine whether the dedicated CRM session contains
    Django authentication state.

    IMPORTANT:

    Do NOT use request.user.is_authenticated here.

    request.user belongs to Django's normal authentication
    middleware/session and can remain authenticated even after
    the dedicated CRM session has been cleared.
    """

    session = get_crm_session(request)

    return bool(
        session.get("_auth_user_id")
        and session.get("_auth_user_backend")
        and session.get("_auth_user_hash")
    )


def get_crm_authenticated_user(request):
    """
    Resolve the user stored inside the dedicated CRM session.

    Returns:

        User instance

    or:

        None
    """

    session = get_crm_session(request)

    user_id = session.get("_auth_user_id")
    backend_path = session.get("_auth_user_backend")

    if not user_id or not backend_path:
        return None

    try:
        from django.contrib.auth import load_backend

        backend = load_backend(backend_path)

        user = backend.get_user(user_id)

        if user is None:
            return None

        # ----------------------------------------------------
        # Verify session auth hash.
        # ----------------------------------------------------

        session_hash = session.get("_auth_user_hash")

        if session_hash:
            if not user.get_session_auth_hash() == session_hash:
                return None

        return user

    except Exception:
        logger.exception(
            "Unable to resolve CRM authenticated user."
        )

        return None


def crm_login_required(view_func):
    """
    Dedicated CRM authentication decorator.

    This is intentionally separate from Django's standard
    @login_required because SHVYA CRM uses its own session
    cookie.

    If the CRM session is missing, redirect to:

        /dashboard/login/
    """

    def wrapped_view(request, *args, **kwargs):

        user = get_crm_authenticated_user(request)

        if user is None:

            return redirect(
                "crm-login"
            )

        # ----------------------------------------------------
        # Make the CRM user available to the view.
        #
        # This avoids depending on Django's global session.
        # ----------------------------------------------------

        request.crm_user = user

        return view_func(
            request,
            *args,
            **kwargs,
        )

    wrapped_view.__name__ = view_func.__name__
    wrapped_view.__doc__ = view_func.__doc__
    wrapped_view.__module__ = view_func.__module__

    return wrapped_view


# ============================================================
# CRM LOGIN
# ============================================================


def crm_login_view(request):
    """
    Dedicated SHVYA CRM login page.

    CRM authentication is stored inside the dedicated
    SHVYA CRM session:

        shvya_crm_sessionid

    This keeps CRM authentication independent from:

        - Django Admin
        - Superadmin
        - other SHVYA sessions
    """

    # ========================================================
    # ALREADY AUTHENTICATED
    # ========================================================

    crm_user = get_crm_authenticated_user(request)

    if crm_user is not None:

        return redirect(
            "crm-dashboard"
        )

    # ========================================================
    # LOGIN SUBMISSION
    # ========================================================

    if request.method == "POST":

        email = request.POST.get(
            "email",
            "",
        ).strip()

        password = request.POST.get(
            "password",
            "",
        )

        # ----------------------------------------------------
        # Basic validation
        # ----------------------------------------------------

        if not email:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Please enter your email address."
                    ),
                    "email": email,
                },
            )

        if not password:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Please enter your password."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Authenticate using Django's authentication backend.
        #
        # User.USERNAME_FIELD = "email"
        # ----------------------------------------------------

        user = authenticate(
            request=request,
            username=email,
            password=password,
        )

        # ----------------------------------------------------
        # Invalid credentials
        # ----------------------------------------------------

        if user is None:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Invalid email or password."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # User disabled
        # ----------------------------------------------------

        if not user.is_active:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your account is currently disabled. "
                        "Please contact your administrator."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Organization validation
        # ----------------------------------------------------

        organization = user.organization

        if organization is None:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your account is not associated "
                        "with an organization."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Organization disabled
        # ----------------------------------------------------

        if not organization.is_active:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your organization account is "
                        "currently disabled. "
                        "Please contact your administrator."
                    ),
                    "email": email,
                },
            )

        # ====================================================
        # LOAD DEDICATED CRM SESSION
        # ====================================================

        crm_session = get_crm_session(
            request
        )

        # ====================================================
        # AUTHENTICATE USER INTO CRM SESSION
        # ====================================================

        set_authenticated_user(
            crm_session,
            user,
        )

        # ====================================================
        # REDIRECT TO DASHBOARD
        # ====================================================

        response = redirect(
            "crm-dashboard"
        )

        # ====================================================
        # SAVE DEDICATED CRM COOKIE
        # ====================================================

        save_session_cookie(
            request,
            response,
            crm_session,
            CRM_SESSION_AREA,
        )

        # ====================================================
        # PREVENT LOGIN PAGE CACHING
        # ====================================================

        response["Cache-Control"] = (
            "no-cache, no-store, must-revalidate"
        )

        response["Pragma"] = "no-cache"
        response["Expires"] = "0"

        return response

    # ========================================================
    # GET — SHOW LOGIN PAGE
    # ========================================================

    response = render(
        request,
        "crm/login.html",
        {
            "email": "",
        },
    )

    response["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


    # ============================================================
# CRM PROFILE
# ============================================================


@crm_login_required
def crm_profile_view(request):
    """
    Display the profile of the currently authenticated CRM user.

    CRM authentication is resolved through the dedicated
    SHVYA CRM session and exposed as:

        request.crm_user

    This intentionally does not rely on request.user because
    SHVYA CRM authentication is isolated from other SHVYA areas.
    """

    user = request.crm_user

    organization = user.organization

    return render(
        request,
        "crm/profile.html",
        {
            "profile_user": user,
            "organization": organization,
        },
    )


# ============================================================
# PHASE 2 — API VIEWS
# ============================================================
#
# NOTE: CRM logout lives in apps.accounts.views.crm_logout_view
# (routed as "crm-logout" in apps.accounts.urls). It flushes the
# entire session rather than clearing individual keys, which is
# the stronger/correct approach — an earlier, weaker duplicate of
# this view previously lived here unused and has been removed.


