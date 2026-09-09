from django.apps import AppConfig


class AiEngagementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai_engagement"

    def ready(self):
        # Register application-controlled qualification-state hooks.
        from . import signals  # noqa: F401

        # Queue slow summary/qualification enrichment independently from the
        # customer-response path for both Meta API and Hosted WhatsApp.
        from . import background_signals  # noqa: F401

        # Fixed prompts are version controlled with the backend. Organization
        # personalization remains in OrgInfo and Knowledge Base content remains
        # in RAG rather than being copied into system prompts.
        from apps.ai_engagement.prompts import (
            INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS,
            QUALIFICATION_SUMMARY_INSTRUCTIONS,
        )
        from apps.ai_engagement.services.internal_summary import (
            InternalSummaryService,
        )
        from apps.ai_engagement.services.qualification import (
            QualificationService,
        )

        InternalSummaryService.SUMMARY_INSTRUCTIONS = (
            INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS
        )
        QualificationService.QUALIFICATION_INSTRUCTIONS = (
            QUALIFICATION_SUMMARY_INSTRUCTIONS
        )

        # Preserve existing channel service entry points while installing the
        # deterministic orchestration policy: short debounce, no generic
        # positive-keyword stage movement, throttled internal enrichment, and
        # consistent Hosted/Meta AI timing.
        from services.channels.ai_orchestration_hooks import (
            install_ai_orchestration_hooks,
        )

        install_ai_orchestration_hooks()
