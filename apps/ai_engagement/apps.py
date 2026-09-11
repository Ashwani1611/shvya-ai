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

        # Fixed task prompts are version controlled with the backend. OrgInfo
        # remains the organization-specific configuration source and Knowledge
        # Base content remains in RAG.
        from apps.ai_engagement.services.prompt_overrides import (
            install_fixed_prompt_overrides,
        )

        install_fixed_prompt_overrides()

        # Keep AI Setup authoring flexible while compiling it into deterministic
        # runtime behavior: option-aware qualification answers, majority-mode
        # evaluation, described-stage transitions, anti-repeat validation, and
        # short file/knowledge intent retrieval.
        from apps.ai_engagement.services.ai_setup_runtime_fixes import (
            install_ai_setup_runtime_fixes,
        )

        install_ai_setup_runtime_fixes()

        # LangGraph is the turn-level orchestration authority. It keeps the
        # existing EngagementService/Celery/WhatsApp contracts stable while
        # segmenting context, deterministic extraction, routing, RAG,
        # generation and validation into explicit nodes.
        from apps.ai_engagement.services.langgraph_orchestration import (
            install_langgraph_orchestration,
        )

        install_langgraph_orchestration()

        # Preserve existing channel entry points while installing deterministic
        # orchestration policy: short debounce, no generic positive-keyword
        # stage movement, throttled enrichment, and consistent Hosted/Meta AI
        # timing.
        from services.channels.ai_orchestration_hooks import (
            install_ai_orchestration_hooks,
        )

        install_ai_orchestration_hooks()
