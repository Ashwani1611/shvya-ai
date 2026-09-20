from functools import wraps

from django.http import HttpResponseForbidden

from apps.accounts.models import User
from apps.crm.authentication import crm_login_required


CONNECT_HUB_ADMIN_ERROR = (
    "Only organization admins can manage Connect Hub integrations."
)


def is_connect_hub_admin(user):
    """Return True only for an active organization administrator."""
    return bool(
        user
        and getattr(user, "is_active", False)
        and not getattr(user, "is_superuser", False)
        and getattr(user, "organization_id", None)
        and getattr(user, "role", None) == User.Role.ADMIN
    )


def connect_hub_admin_required(view_func):
    """Require the dedicated CRM session plus organization-admin authority.

    Connect Hub credentials and configuration are organization-scoped. Pipeline
    permissions, including can_manage_api_keys, must not grant access because a
    pipeline-scoped permission cannot safely authorize organization-wide API
    keys, webhooks, SMTP credentials, Sheets sync, or Meta Lead Ads settings.
    """

    @wraps(view_func)
    def protected(request, *args, **kwargs):
        if not is_connect_hub_admin(getattr(request, "crm_user", None)):
            return HttpResponseForbidden(CONNECT_HUB_ADMIN_ERROR)
        return view_func(request, *args, **kwargs)

    return crm_login_required(protected)
