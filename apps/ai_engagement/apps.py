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

        from apps.ai_engagement.services.conditional_qualification_runtime import (
            install_conditional_qualification_runtime,
        )
        install_conditional_qualification_runtime()

        from apps.ai_engagement.services.conditional_state_postfix import (
            install_conditional_state_postfix,
        )
        install_conditional_state_postfix()

        from apps.ai_engagement.services.prompt_overrides import (
            install_fixed_prompt_overrides,
        )
        install_fixed_prompt_overrides()

        from apps.ai_engagement.services.ai_setup_runtime_fixes import (
            install_ai_setup_runtime_fixes,
        )
        install_ai_setup_runtime_fixes()

        from apps.ai_engagement.services.conversation_priority_runtime import (
            install_conversation_priority_runtime,
        )
        install_conversation_priority_runtime()

        from apps.ai_engagement.services.pipeline_context_guard import (
            install_pipeline_context_guard,
        )
        install_pipeline_context_guard()

        from apps.ai_engagement.services.ai_setup_runtime_compat import (
            install_ai_setup_runtime_compat,
        )
        install_ai_setup_runtime_compat()

        # Bridge qualification answers into deterministic CRM writes.
        from apps.ai_engagement.services.qualification_crm_action_runtime import (
            install_qualification_crm_action_runtime,
        )
        install_qualification_crm_action_runtime()

        # Accept normal grounded date/time language without inventing a time.
        from apps.ai_engagement.services.reminder_time_runtime import (
            install_reminder_time_runtime,
        )
        install_reminder_time_runtime()

        from apps.ai_engagement.services.crm_routing_reliability import (
            install_crm_routing_reliability,
        )
        install_crm_routing_reliability()

        from apps.ai_engagement.services.crm_action_projection_fix import (
            install_crm_action_projection_fix,
        )
        install_crm_action_projection_fix()

        # Compile ##Qualification criteria / ##Stage shifting /
        # ##Attribute mapped into shared Hosted + Meta API backend policy.
        from apps.ai_engagement.services.engagement_instruction_runtime import (
            install_engagement_instruction_runtime,
        )
        install_engagement_instruction_runtime()

        # Qualified can only come from deterministic completed qualification;
        # never let a model-selected Qualified target become generic routing.
        from apps.ai_engagement.services.qualified_transition_guard import (
            install_qualified_transition_guard,
        )
        install_qualified_transition_guard()

        from apps.ai_engagement.services.langgraph_orchestration import (
            install_langgraph_orchestration,
        )
        install_langgraph_orchestration()

        # Keep qualification active through New Lead -> In Conversation while
        # making normal acknowledgements, call requests, and completed flows
        # conversational instead of restarting the questionnaire or falling back.
        from apps.ai_engagement.services.natural_conversation_runtime import (
            install_natural_conversation_runtime,
        )
        install_natural_conversation_runtime()

        from apps.ai_engagement.services.engagement_failsoft import (
            install_engagement_failsoft,
        )
        install_engagement_failsoft()

        from apps.ai_engagement.services.task_execution_failsoft import (
            install_task_execution_failsoft,
        )
        install_task_execution_failsoft()

        from services.channels.ai_orchestration_hooks import (
            install_ai_orchestration_hooks,
        )
        install_ai_orchestration_hooks()
