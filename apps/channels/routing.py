"""
WebSocket URL routes for apps.channels. Mounted at /ws/ by
config/asgi.py -- kept separate from apps/channels/urls.py since
that file is for ordinary HTTP views only.
"""

from django.urls import re_path

from . import consumers

websocket_urlpatterns = [

    re_path(
        r"^ws/whatsapp/inbox/$",
        consumers.WhatsAppChatConsumer.as_asgi(),
    ),

    re_path(
        r"^ws/whatsapp/(?P<lead_id>[0-9a-fA-F-]+)/$",
        consumers.WhatsAppChatConsumer.as_asgi(),
    ),

]