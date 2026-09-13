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

        # Mirror the latest rolling conversation summary into one system Lead
        # Note. The versioned InternalConversationSummary remains authoritative;
        # this is a CRM visibility surface alongside qualification notes.
        from . import summary_note_signals  # noqa: F401

        # Compile machine-evaluable conditional qualification rules and install
        # eligibility/NOT_APPLICABLE, conversation-mode, state-recovery and
        # atomic/idempotent state guards before EngagementService, graph and
        # prompt wrappers bind qualification helpers at module import time.
        from apps.ai_engagement.services.conditional_qualification_runtime import (
            install_conditional_qualification_runtime,
        )

        install_conditional_qualification_runtime()

        # Re-normalize derived state after an answer flips qualification into a
        # terminal mode so returned/persisted conversation_mode cannot lag the
        # completion transition by one turn.
        from apps.ai_engagement.services.conditional_state_postfix import (
            install_conditional_state_postfix,
        )

        install_conditional_state_postfix()

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

        # Mark the persisted pipeline as current and every cross-pipeline routing
        # candidate as internal-only so the model cannot confuse CRM metadata
        # with customer-facing organization facts.
        from apps.ai_engagement.services.pipeline_context_guard import (
            install_pipeline_context_guard,
        )

        install_pipeline_context_guard()

        # Some bounded internal/test contexts omit the current stage because no
        # stage operation is expected. Keep the transition wrapper compatible
        # with those callers without weakening validation in full AIContext.
        from apps.ai_engagement.services.ai_setup_runtime_compat import (
            install_ai_setup_runtime_compat,
        )

        install_ai_setup_runtime_compat()

        # LangGraph is the turn-level orchestration authority. It keeps the
        # existing EngagementService/Celery/WhatsApp contracts stable while
        # segmenting context, deterministic extraction, routing, RAG,
        # generation and validation into explicit nodes.
        from apps.ai_engagement.services.langgraph_orchestration import (
            install_langgraph_orchestration,
        )

        install_langgraph_orchestration()

        # After every normal graph/provider/repair layer is installed, add one
        # deterministic last-resort response. A malformed provider result must
        # not leave a genuine inbound WhatsApp turn unanswered.
        from apps.ai_engagement.services.engagement_failsoft import (
            install_engagement_failsoft,
        )

        install_engagement_failsoft()

        # Defense in depth at the Celery execution boundary: if any permanent
        # EngagementError still reaches the canonical task's terminal
        # engagement_generation_failed result, finalize a deterministic reply
        # through the normal permission/freshness/idempotency/send checks.
        from apps.ai_engagement.services.task_execution_failsoft import (
            install_task_execution_failsoft,
        )

        install_task_execution_failsoft()

        # Preserve existing channel entry points while installing deterministic
        # orchestration policy: short debounce, no generic positive-keyword
        # stage movement, throttled enrichment, and consistent Hosted/Meta AI
        # timing.
        from services.channels.ai_orchestration_hooks import (
            install_ai_orchestration_hooks,
        )

        install_ai_orchestration_hooks()
