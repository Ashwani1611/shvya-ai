from django.urls import path
from django.views.generic import RedirectView

from apps.channels import instagram_ui
from apps.core.coming_soon import coming_soon
from apps.crm.views.ai_setup import ai_setup_view
from apps.crm.views.ai_trace import (
    ai_trace_detail_api,
    ai_trace_detail_view,
    ai_trace_list_view,
)
from apps.crm.views.faq import faq_view


coming_soon_urlpatterns = [
    # Call tools now live inside Connect Hub.
    path(
        "connect-hub/call-scheduler/",
        coming_soon,
        {"feature": "call-scheduler"},
        name="crm-call-scheduler",
    ),
    path(
        "connect-hub/call-tracker/",
        coming_soon,
        {"feature": "call-tracker"},
        name="crm-call-tracker",
    ),

    # Keep older direct call-tool links working.
    path(
        "call-scheduler/",
        RedirectView.as_view(
            pattern_name="crm-call-scheduler",
            permanent=True,
            query_string=True,
        ),
    ),
    path(
        "call-tracker/",
        RedirectView.as_view(
            pattern_name="crm-call-tracker",
            permanent=True,
            query_string=True,
        ),
    ),

    # Cadence Touchpoints remains a future phase. Sequences is implemented
    # by apps.followups and owns /dashboard/cadence/sequences/.
    path(
        "cadence/touchpoints/",
        coming_soon,
        {"feature": "auto-follow-ups-touchpoints"},
        name="crm-auto-follow-ups-touchpoints",
    ),
    # Preserve existing bookmarks from the older Auto Follow-ups routes.
    path(
        "auto-follow-ups/touchpoints/",
        RedirectView.as_view(
            pattern_name="crm-auto-follow-ups-touchpoints",
            permanent=True,
            query_string=True,
        ),
    ),
    path(
        "auto-follow-ups/workflows/",
        RedirectView.as_view(
            pattern_name="crm-auto-follow-ups-touchpoints",
            permanent=True,
            query_string=True,
        ),
    ),

    # Playbooks replaces Knowledge Base in the dashboard navigation and URL.
    path(
        "playbooks/ai-setup/",
        ai_setup_view,
        name="crm-knowledge-base-ai-setup",
    ),
    path(
        "playbooks/ai-setup/activity/",
        ai_trace_list_view,
        name="crm-ai-trace-list",
    ),
    path(
        "playbooks/ai-setup/activity/<uuid:trace_id>/",
        ai_trace_detail_view,
        name="crm-ai-trace-detail",
    ),
    path(
        "playbooks/ai-setup/activity/<uuid:trace_id>/api/",
        ai_trace_detail_api,
        name="crm-ai-trace-detail-api",
    ),
    path(
        "playbooks/faq/",
        faq_view,
        name="crm-knowledge-base-faq",
    ),

    # Support Portal is available from the sidebar below Teams.
    path(
        "support-portal/",
        coming_soon,
        {"feature": "support-portal"},
        name="crm-support-portal",
    ),

    # Instagram professional messaging.
    path(
        "instagram/connect/",
        instagram_ui.instagram_connect_view,
        name="crm-instagram-connect",
    ),
    path(
        "instagram/connect/start/",
        instagram_ui.instagram_oauth_start_view,
        name="crm-instagram-oauth-start",
    ),
    path(
        "instagram/connect/return/",
        instagram_ui.instagram_oauth_return_view,
        name="crm-instagram-oauth-return",
    ),
    path(
        "instagram/disconnect/",
        instagram_ui.instagram_disconnect_view,
        name="crm-instagram-disconnect",
    ),
    path(
        "instagram/chats/",
        instagram_ui.instagram_chat_list_view,
        name="crm-instagram-chats",
    ),
    path(
        "instagram/chats/<str:conversation_id>/",
        instagram_ui.instagram_chat_detail_view,
        name="crm-instagram-chat-detail",
    ),
    path(
        "instagram/chats/<str:conversation_id>/send/",
        instagram_ui.instagram_send_message_view,
        name="crm-instagram-send-message",
    ),
]
