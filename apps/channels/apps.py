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
        from . import hosted_ignore_models  # noqa: F401
        from . import hosted_lifecycle  # noqa: F401
        from . import instagram_models  # noqa: F401
        from . import template_models  # noqa: F401
        from . import lead_source_signals  # noqa: F401
        from . import campaign_models  # noqa: F401

        # Register channel background tasks in both web and worker startup.
        # Task bodies import providers lazily, so importing these modules is safe
        # during Django app initialization.
        from . import hosted_tasks  # noqa: F401
        from . import instagram_tasks  # noqa: F401
        from . import welcome_tasks  # noqa: F401
        from . import campaign_tasks  # noqa: F401

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
        from services.channels.whatsapp_phone_registration import (
            install_whatsapp_phone_registration,
        )
        from services.channels.whatsapp_api_runtime import (
            install_whatsapp_api_runtime,
        )
        from services.channels.whatsapp_coexistence_runtime import (
            install_whatsapp_coexistence_runtime,
        )
        from services.channels.whatsapp_coexistence_partner_runtime import (
            install_whatsapp_coexistence_partner_runtime,
        )
        from services.channels.whatsapp_coexistence_asset_resolution import (
            install_whatsapp_coexistence_asset_resolution,
        )
        from services.channels.instagram_runtime import install_instagram_runtime
        from services.channels.staging_outbound_safety import (
            install_staging_outbound_safety,
        )
        from services.channels.campaign_events import install_campaign_webhook

        install_instagram_runtime()
        install_whatsapp_phone_registration()
        install_whatsapp_template_transport()
        install_hosted_whatsapp_transport()
        install_whatsapp_failure_diagnostics()
        install_whatsapp_api_runtime()
        # Install Coexistence asset recovery before request handling. Meta's
        # WhatsApp Business App onboarding event can omit phone_number_id even
        # after a successful selection; the wrapper resolves the unique
        # is_on_biz_app candidate without changing normal Connect API behavior.
        install_whatsapp_coexistence_asset_resolution()
        # Install Coexistence handling last so both layers sit behind existing
        # webhook signature verification and standard API message processing.
        install_whatsapp_coexistence_runtime()
        install_whatsapp_coexistence_partner_runtime()
        install_campaign_webhook()
        # This must be the final transport wrapper. Production is unchanged;
        # staging can only send to explicitly allowlisted test recipients.
        install_staging_outbound_safety()
