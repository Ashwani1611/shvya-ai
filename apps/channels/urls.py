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
        "webhook/",
        views_flat.whatsapp_webhook_view,
        name="whatsapp-webhook",
    ),
    path(
        "send/<uuid:lead_id>/",
        views_flat.whatsapp_send_message_view,
        name="whatsapp-send-message",
    ),
]