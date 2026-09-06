from django.urls import path

from . import connection_ui
from . import template_action_ui
from . import template_ui
from . import views_flat
from . import whatsapp_chat_failure_ui
from . import whatsapp_template_send_ui
from . import whatsapp_ui

urlpatterns = [
    path("accounts/", views_flat.whatsapp_account_list_view, name="whatsapp-accounts"),
    path("connect/", views_flat.whatsapp_connect_choice_view, name="whatsapp-connect-choice"),
    path("connect/api/", connection_ui.whatsapp_connect_api_view, name="whatsapp-connect-api"),
    path("connect/api/attempt-event/", connection_ui.whatsapp_connection_attempt_event_view, name="whatsapp-connection-attempt-event"),
    path("connect/api/embedded-signup/", connection_ui.whatsapp_embedded_signup_callback_view, name="whatsapp-embedded-signup-callback"),
    path("connect/hosted/", views_flat.whatsapp_connect_hosted_view, name="whatsapp-connect-hosted"),
    path("accounts/<uuid:account_id>/disconnect/", views_flat.whatsapp_disconnect_view, name="whatsapp-disconnect"),
    path("accounts/<uuid:account_id>/resubscribe/", views_flat.whatsapp_resubscribe_view, name="whatsapp-resubscribe"),
    path("send/<uuid:lead_id>/", views_flat.whatsapp_send_message_view, name="whatsapp-send-message"),
    path("campaigns/", views_flat.whatsapp_campaign_list_view, name="whatsapp-campaign-list"),
    path("campaigns/new/", views_flat.whatsapp_campaign_create_view, name="whatsapp-campaign-create"),
    path("campaigns/<uuid:campaign_id>/", views_flat.whatsapp_campaign_detail_view, name="whatsapp-campaign-detail"),
    path("campaigns/<uuid:campaign_id>/launch/", views_flat.whatsapp_campaign_launch_view, name="whatsapp-campaign-launch"),

    # Real WABA template management. The old views in views_flat are retained
    # only for compatibility with imports; active routes use the focused flow.
    # Editor routes preserve the clicked submit action before the page's
    # duplicate-submit guard disables the buttons. The list refreshes pending
    # templates from Meta so approval/rejection is reflected on reload.
    path("templates/", template_action_ui.template_list, name="whatsapp-template-list"),
    path("templates/new/", template_action_ui.template_create, name="whatsapp-template-create"),
    path("templates/<uuid:template_id>/edit/", template_action_ui.template_edit, name="whatsapp-template-edit"),
    path("templates/<uuid:template_id>/submit/", template_ui.template_submit, name="whatsapp-template-submit"),
    path("templates/<uuid:template_id>/copy/", template_ui.template_copy, name="whatsapp-template-copy"),
    path("templates/<uuid:template_id>/delete/", template_ui.template_delete, name="whatsapp-template-delete"),
    path("templates/sync/", template_ui.template_sync, name="whatsapp-template-sync"),
    path("templates/placeholders/", template_ui.template_placeholders, name="whatsapp-template-placeholders"),

    path("chats/", views_flat.whatsapp_chat_list_view, name="whatsapp-chats"),
    path("chats/<uuid:lead_id>/", whatsapp_chat_failure_ui.whatsapp_chat_detail_view, name="whatsapp-chat-detail"),
    path("send-template/<uuid:lead_id>/", whatsapp_template_send_ui.whatsapp_send_template_view, name="whatsapp-send-template"),
    path("leads/<uuid:lead_id>/calls.json", views_flat.whatsapp_lead_calls_json, name="whatsapp-lead-calls-json"),
    path("leads/<uuid:lead_id>/pipeline-options/", whatsapp_ui.whatsapp_lead_pipeline_options_view, name="whatsapp-lead-pipeline-options"),
    path("leads/<uuid:lead_id>/quick-update/", whatsapp_ui.whatsapp_lead_quick_update_view, name="whatsapp-lead-quick-update"),
    path("leads/<uuid:lead_id>/ai-toggle/", whatsapp_ui.whatsapp_lead_ai_toggle_view, name="whatsapp-lead-ai-toggle"),
    path("leads/<uuid:lead_id>/attributes/save/", whatsapp_ui.whatsapp_lead_attributes_save_view, name="whatsapp-lead-attributes-save"),
]
