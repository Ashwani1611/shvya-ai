from .base import *  # noqa

MIDDLEWARE = [
    *MIDDLEWARE,
    "apps.core.middleware.GlobalToastMiddleware",
]
