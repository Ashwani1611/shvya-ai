from django.urls import path

from apps.core.coming_soon import coming_soon


coming_soon_urlpatterns = [
    path("copilot/", coming_soon, {"feature": "copilot"}, name="crm-copilot"),
    path("smart-triggers/", coming_soon, {"feature": "smart-triggers"}, name="crm-smart-triggers"),
    path("analytics/", coming_soon, {"feature": "analytics"}, name="crm-analytics"),
    path("integrations-hub/", coming_soon, {"feature": "integrations-hub"}, name="crm-integrations-hub"),
    path("call-scheduler/", coming_soon, {"feature": "call-scheduler"}, name="crm-call-scheduler"),
    path("call-tracker/", coming_soon, {"feature": "call-tracker"}, name="crm-call-tracker"),
    path("teams/", coming_soon, {"feature": "teams"}, name="crm-teams"),

    # Auto Follow-ups
    path("auto-follow-ups/sequences/", coming_soon, {"feature": "auto-follow-ups-sequences"}, name="crm-auto-follow-ups-sequences"),
    path("auto-follow-ups/workflows/", coming_soon, {"feature": "auto-follow-ups-workflows"}, name="crm-auto-follow-ups-workflows"),

    # Knowledge Base
    path("knowledge-base/ai-setup/", coming_soon, {"feature": "knowledge-base-ai-setup"}, name="crm-knowledge-base-ai-setup"),
    path("knowledge-base/faq/", coming_soon, {"feature": "knowledge-base-faq"}, name="crm-knowledge-base-faq"),

    # Instagram
    path("instagram/connect/", coming_soon, {"feature": "instagram-connect"}, name="crm-instagram-connect"),
    path("instagram/chats/", coming_soon, {"feature": "instagram-chats"}, name="crm-instagram-chats"),
]
