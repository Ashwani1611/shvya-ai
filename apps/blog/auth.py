"""Blog authority is platform-only, regardless of tenant Django permissions."""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def _platform_actor(user):
    return bool(user and user.is_authenticated and user.is_active
                and user.organization_id is None
                and (user.is_superuser or user.role == "superadmin"))


def can_edit(user):
    return bool(_platform_actor(user) and (user.is_superuser or user.has_perm("blog.change_post")))


def can_publish(user):
    # Publishing is an additional capability of an editor, including when a
    # scheduled job rechecks the actor after their account permissions change.
    return bool(can_edit(user) and (user.is_superuser or user.has_perm("blog.publish_post")))


def check_authorized(user, *, publish=False):
    if not (can_publish(user) if publish else can_edit(user)):
        raise PermissionDenied("Website blog access requires platform editorial permission.")


def editorial_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url="/superadmin/blog/login/")
        check_authorized(request.user)
        return view(request, *args, **kwargs)
    return wrapped
