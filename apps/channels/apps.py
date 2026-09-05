from django.apps import AppConfig


class ChannelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.channels"
    label = "channels"

    def ready(self):
        # WhatsAppConnectionAttempt lives in a focused module so the existing
        # large channels/models.py file does not need to carry connection-flow
        # audit concerns. Importing it here registers the model with Django at
        # app startup, just like importing signal modules from ready().
        from . import connection_attempts  # noqa: F401
