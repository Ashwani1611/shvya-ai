from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crm"
    label = "crm"

    def ready(self):
        # Startup registers model signals only. Runtime behavior belongs in the
        # owning services/views and must not be replaced process-wide here.
        from .models import signals  # noqa: F401
