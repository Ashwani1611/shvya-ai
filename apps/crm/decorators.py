from functools import wraps

from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
    get_user_model,
)
from django.shortcuts import redirect

from apps.accounts.session_utils import get_session_store


def crm_login_required(view_func):
    """
    Require authentication through the dedicated SHVYA CRM session.

    CRM authentication is stored in:

        shvya_crm_sessionid

    This is intentionally separate from Django's normal
    sessionid cookie.
    """

    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):

        # =====================================================
        # LOAD CRM SESSION
        # =====================================================

        crm_session = get_session_store(
            request,
            "dashboard",
        )

        # =====================================================
        # READ DJANGO AUTHENTICATION STATE
        # =====================================================

        user_id = crm_session.get(
            SESSION_KEY,
        )

        backend = crm_session.get(
            BACKEND_SESSION_KEY,
        )

        session_hash = crm_session.get(
            HASH_SESSION_KEY,
        )

        # =====================================================
        # NO CRM LOGIN
        # =====================================================

        if not user_id or not backend or not session_hash:
            return redirect(
                "/dashboard/login/",
            )

        # =====================================================
        # LOAD USER
        # =====================================================

        User = get_user_model()

        try:
            user = User.objects.get(
                pk=user_id,
            )

        except User.DoesNotExist:
            return redirect(
                "/dashboard/login/",
            )

        # =====================================================
        # USER MUST BE ACTIVE
        # =====================================================

        if not user.is_active:
            return redirect(
                "/dashboard/login/",
            )

        # =====================================================
        # VALIDATE SESSION HASH
        # =====================================================

        if user.get_session_auth_hash() != session_hash:
            return redirect(
                "/dashboard/login/",
            )

        # =====================================================
        # ATTACH CRM USER TO REQUEST
        # =====================================================

        request.user = user

        # =====================================================
        # CONTINUE TO VIEW
        # =====================================================

        return view_func(
            request,
            *args,
            **kwargs,
        )

    return wrapped_view