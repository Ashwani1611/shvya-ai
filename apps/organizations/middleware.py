"""Enforce package access before dashboard views can read or mutate data."""

from django.http import JsonResponse
from django.shortcuts import render
from .features import MODULE_LABELS, module_enabled, module_for_path


def package_denial_response(request, user):
    module = module_for_path(request.path_info)
    if module and user and user.is_authenticated and not user.is_superuser:
        organization = getattr(user, "organization", None)
        if organization and not module_enabled(organization, module):
            if (
                request.method != "GET"
                or "application/json" in request.headers.get("Accept", "")
                or request.path_info.startswith("/api/")
            ):
                return JsonResponse(
                    {"detail": "Upgrade to unlock", "module": module}, status=403
                )
            return render(
                request,
                "organizations/upgrade.html",
                {
                    "module_label": MODULE_LABELS[module],
                },
                status=403,
            )
    return None


class PackageAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        denied = package_denial_response(request, getattr(request, "user", None))
        return denied if denied is not None else self.get_response(request)
