"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Django's app registry must be fully set up (via
# get_asgi_application()) BEFORE anything below imports Channels
# routing or consumers, since those modules import Django models.
django_asgi_app = get_asgi_application()

from channels.routing import (  # noqa: E402
    ProtocolTypeRouter,
    URLRouter,
)

from apps.accounts.channels_middleware import (  # noqa: E402
    CRMSessionAuthMiddleware,
)
from apps.channels.routing import (  # noqa: E402
    websocket_urlpatterns,
)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,

        "websocket": CRMSessionAuthMiddleware(
            URLRouter(
                websocket_urlpatterns,
            )
        ),
    }
)