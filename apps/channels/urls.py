from django.urls import path

from . import views_flat

urlpatterns = [
    path(
        "settings/",
        views_flat.whatsapp_settings_view,
        name="whatsapp-settings",
    ),
    path(
        "connect/",
        views_flat.whatsapp_connect_choice_view,
        name="whatsapp-connect-choice",
    ),
    path(
        "connect/api/",
        views_flat.whatsapp_connect_api_view,
        name="whatsapp-connect-api",
    ),
    path(
        "connect/hosted/",
        views_flat.whatsapp_connect_hosted_view,
        name="whatsapp-connect-hosted",
    ),
    path(
        "disconnect/",
        views_flat.whatsapp_disconnect_view,
        name="whatsapp-disconnect",
    ),
    path(
        "send/<uuid:lead_id>/",
        views_flat.whatsapp_send_message_view,
        name="whatsapp-send-message",
    ),
    # path(
    #     "campaigns/",
    #     views_flat.whatsapp_campaign_list_view,
    #     name="whatsapp-campaign-list",
    # ),
    # path(
    #     "campaigns/new/",
    #     views_flat.whatsapp_campaign_create_view,
    #     name="whatsapp-campaign-create",
    # ),
    # path(
    #     "campaigns/<uuid:campaign_id>/",
    #     views_flat.whatsapp_campaign_detail_view,
    #     name="whatsapp-campaign-detail",
    # ),
    # path(
    #     "campaigns/<uuid:campaign_id>/launch/",
    #     views_flat.whatsapp_campaign_launch_view,
    #     name="whatsapp-campaign-launch",
    # ),
]