from .base import *  # noqa

MIDDLEWARE = [
    *MIDDLEWARE,
    "apps.core.middleware.GlobalToastMiddleware",
    "apps.core.whatsapp_theme.WhatsAppThemeMiddleware",
]
