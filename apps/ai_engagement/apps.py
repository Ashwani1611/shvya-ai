from django.apps import AppConfig


class AiEngagementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai_engagement"

    def ready(self):
        # Register inbound WhatsApp -> AI engagement event hooks.
        # Imported here to avoid model-import side effects during app loading.
        from . import signals  # noqa: F401
