from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"
    label = "integrations"

    def ready(self):
        from . import signals  # noqa: F401
        from .services.meta_lead_webhook_security import (
            install_meta_lead_webhook_security,
        )

        install_meta_lead_webhook_security()
