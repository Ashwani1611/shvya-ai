from django.apps import AppConfig


class TelephonyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.telephony"
    label = "telephony"
    verbose_name = "Call Intelligence"

    def ready(self):
        from . import signals  # noqa: F401
