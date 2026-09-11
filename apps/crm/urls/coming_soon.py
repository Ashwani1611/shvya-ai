from django.urls import path
from django.views.generic import RedirectView

from apps.core.coming_soon import coming_soon


coming_soon_urlpatterns = [
    path("call-scheduler/", coming_soon, {"feature": "call-scheduler"}, name="crm-call-scheduler"),
    path("call-tracker/", coming_soon, {"feature": "call-tracker"}, name="crm-call-tracker"),

    # Auto Follow-ups Touchpoints remains a future phase. Sequences is now
    # implemented by apps.followups and owns /dashboard/auto-follow-ups/sequences/.
    path("auto-follow-ups/touchpoints/", coming_soon, {"feature": "auto-follow-ups-touchpoints"}, name="crm-auto-follow-ups-touchpoints"),
    # Preserve existing bookmarks while navigation uses the new route.
    path(
        "auto-follow-ups/workflows/",
        RedirectView.as_view(
            pattern_name="crm-auto-follow-ups-touchpoints",
            permanent=True,
            query_string=True,
        ),
    ),

    # Instagram
    path("instagram/connect/", coming_soon, {"feature": "instagram-connect"}, name="crm-instagram-connect"),
    path("instagram/chats/", coming_soon, {"feature": "instagram-chats"}, name="crm-instagram-chats"),
]
