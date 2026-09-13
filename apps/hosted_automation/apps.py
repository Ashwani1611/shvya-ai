from django.apps import AppConfig


class HostedAutomationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hosted_automation"
    verbose_name = "Hosted WhatsApp Automation"

    def ready(self):
        # Hosted AI enqueueing is owned by the persisted-message signal and
        # execution receives its account/source context explicitly. No Celery
        # task or sender methods are replaced process-wide.
        from . import signals  # noqa: F401
