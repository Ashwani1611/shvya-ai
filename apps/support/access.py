from functools import wraps

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from apps.organizations.access import crm_user_is_authorized
from .models import OrganizationSupportPolicy, Ticket
from .policy import platform_staff


def staff_users():
    return get_user_model().objects.filter(is_active=True, is_staff=True, is_superuser=True,
                                           organization__isnull=True)


def customer_authorized(user):
    return bool(getattr(user, "is_authenticated", False) and crm_user_is_authorized(user))


def own_only(user):
    if not customer_authorized(user):
        return True
    return OrganizationSupportPolicy.objects.filter(organization_id=user.organization_id,
                                                      own_tickets_only=True).exists()


def visible_tickets(user):
    if platform_staff(user):
        return Ticket.objects.all()
    if not customer_authorized(user):
        return Ticket.objects.none()
    qs = Ticket.objects.filter(organization_id=user.organization_id)
    if own_only(user) and user.role != "admin":
        qs = qs.filter(requester_id=user.pk)
    return qs


def allowed_pipelines(user):
    # Reuse SHVYA's existing pipeline-permission owner rather than duplicating it.
    from apps.crm.views.api import get_user_pipelines
    return get_user_pipelines(user).filter(organization_id=user.organization_id, is_active=True)


def portal_required(*, staff=False):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            expected = "superadmin" if staff else "dashboard"
            if not getattr(user, "is_authenticated", False):
                return redirect("superadmin-login" if staff else "crm-login")
            if getattr(request, "shvya_session_area", None) != expected:
                raise PermissionDenied("Use the correct SHVYA workspace session.")
            if (staff and not platform_staff(user)) or (not staff and not customer_authorized(user)):
                raise PermissionDenied("You do not have access to this support workspace.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorate
