from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"
    verbose_name = "SHVYA Support"

    def ready(self):
        from . import checks  # noqa: F401
