from django.apps import AppConfig


class ChannelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.channels"
    label = "channels"

    def ready(self):
        # Focused model modules are imported here so Django registers them
        # without making the already-large channels/models.py carry unrelated
        # connection/template operational audit concerns.
        from . import connection_attempts  # noqa: F401
        from . import template_models  # noqa: F401
