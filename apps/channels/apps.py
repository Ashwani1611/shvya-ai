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

        # Register hosted-session Celery tasks in both web and worker startup.
        # The task bodies import their models/providers lazily, so importing the
        # module here is safe during Django app initialization.
        from . import hosted_tasks  # noqa: F401

        # Install the actual Meta template transport first. The failure layer
        # then wraps every transport, including templates, so exact Meta error
        # codes are preserved consistently.
        from services.channels.whatsapp_template_delivery import (
            install_whatsapp_template_transport,
        )
        from services.channels.whatsapp_failure_patch import (
            install_whatsapp_failure_diagnostics,
        )
        from services.channels.hosted_whatsapp_transport import (
            install_hosted_whatsapp_transport,
        )

        install_whatsapp_template_transport()
        install_hosted_whatsapp_transport()
        install_whatsapp_failure_diagnostics()
