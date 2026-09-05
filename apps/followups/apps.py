from django.apps import AppConfig


class FollowupsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.followups"
    label = "followups"

    def ready(self):
        from . import signals  # noqa: F401
