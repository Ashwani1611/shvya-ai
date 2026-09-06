from django.urls import path

from apps.core.coming_soon import coming_soon


coming_soon_urlpatterns = [
    path("integrations-hub/", coming_soon, {"feature": "integrations-hub"}, name="crm-integrations-hub"),
    path("call-scheduler/", coming_soon, {"feature": "call-scheduler"}, name="crm-call-scheduler"),
    path("call-tracker/", coming_soon, {"feature": "call-tracker"}, name="crm-call-tracker"),

    # Auto Follow-ups Workflows remains a future phase. Sequences is now
    # implemented by apps.followups and owns /dashboard/auto-follow-ups/sequences/.
    path("auto-follow-ups/workflows/", coming_soon, {"feature": "auto-follow-ups-workflows"}, name="crm-auto-follow-ups-workflows"),

    # Instagram
    path("instagram/connect/", coming_soon, {"feature": "instagram-connect"}, name="crm-instagram-connect"),
    path("instagram/chats/", coming_soon, {"feature": "instagram-chats"}, name="crm-instagram-chats"),
]
