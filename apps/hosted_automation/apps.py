from django.apps import AppConfig


class HostedAutomationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hosted_automation"
    verbose_name = "Hosted WhatsApp Automation"

    def ready(self):
        from services.channels.hosted_ai_delay import install_hosted_ai_delay_dispatch

        install_hosted_ai_delay_dispatch()
        from . import signals  # noqa: F401
