"""Authenticated, organisation-scoped Google hosting preferences."""
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from apps.crm.authentication import crm_login_required
from apps.organizations.models import Organization
from .google import google_is_configured
from .google_policy import (
    GOOGLE_MODE_CHOICES, ORGANIZATION_ONLY, ORGANIZATION_WITH_FALLBACK,
    save_google_mode, settings_allow_platform_fallback,
)
from .models import CalendarPage, GoogleCalendarConnection
from .platform_google import platform_status
from .views import _require_calendar_manager


class GoogleHostingForm(forms.Form):
    mode = forms.ChoiceField(
        choices=GOOGLE_MODE_CHOICES, widget=forms.RadioSelect,
        label="Google meeting organiser",
    )


@crm_login_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def google_settings(request):
    user = request.crm_user
    _require_calendar_manager(user)
    organization = get_object_or_404(
        Organization, pk=user.organization_id, is_active=True,
    )
    mode = (
        ORGANIZATION_WITH_FALLBACK
        if settings_allow_platform_fallback(organization.settings)
        else ORGANIZATION_ONLY
    )
    form = GoogleHostingForm(
        request.POST if request.method == "POST" else None,
        initial={"mode": mode},
    )
    if request.method == "POST" and form.is_valid():
        try:
            save_google_mode(actor=user, mode=form.cleaned_data["mode"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(
                request,
                "Google hosting preference saved for your organisation. "
                "Existing meetings keep their original organiser.",
            )
            return redirect("shvya_calendar:google_settings")
    platform = platform_status()
    # Only limited, non-secret projections reach the template. Never pass
    # token-bearing connection models or the shared platform account identity.
    connections = list(GoogleCalendarConnection.objects.filter(
        organization_id=user.organization_id,
        user__organization_id=user.organization_id,
        user__is_active=True, is_active=True,
    ).order_by("email").values("email", "user__name", "user_id")[:101])
    pages = Paginator(CalendarPage.objects.filter(
        organization_id=user.organization_id,
    ).select_related("host").order_by("name", "pk"), 25).get_page(
        request.GET.get("page")
    )
    return render(request, "shvya_calendar/google_settings.html", {
        "form": form, "organization_name": organization.name,
        "connections": connections[:100],
        "connections_truncated": len(connections) > 100,
        "pages": pages, "oauth_configured": google_is_configured(),
        "platform_available": platform["enabled"] and platform["configured"],
    }, status=400 if request.method == "POST" else 200)
