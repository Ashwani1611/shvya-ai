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

        # Reminder extraction is independent of qualification mapping authority.
        from apps.ai_engagement.services.reminder_time_runtime import (
            install_reminder_time_runtime,
        )
        install_reminder_time_runtime()

        # Preserve normal-conversation reminders and evidence-bound stage routing
        # without restoring fuzzy qualification mapping or automatic completion
        # stages. Qualification ownership lives in the contract below.
        from apps.ai_engagement.services.crm_routing_reliability import (
            install_crm_routing_reliability,
        )
        install_crm_routing_reliability()

        # Compile organization-authored qualification/stage policy for the shared
        # Hosted + Meta API runtime. Deterministic execution is owned by the
        # qualification execution contract installed below.
        from apps.ai_engagement.services.engagement_instruction_runtime import (
            install_engagement_instruction_runtime,
        )
        install_engagement_instruction_runtime()

        # Install the grounding scope boundary before qualification routing.
        from apps.ai_engagement.services.qualification_grounding_scope_guard import (
            install_qualification_grounding_scope_guard,
        )
        install_qualification_grounding_scope_guard()

        # Normalize direct answers only against the persisted active requirement
        # before generic knowledge handling. This runtime must not choose CRM
        # attributes or completion stages.
        from apps.ai_engagement.services.qualification_answer_routing_runtime import (
            install_qualification_answer_routing_runtime,
        )
        install_qualification_answer_routing_runtime()

        from apps.ai_engagement.services.langgraph_orchestration import (
            install_langgraph_orchestration,
        )
        install_langgraph_orchestration()

        # Reply eligibility is backend-owned. The model may generate language,
        # extraction and proposals, but it cannot suppress a valid inbound turn.
        # Explicit opt-out remains deterministic and permission/transport guards
        # continue to run outside the model.
        from apps.ai_engagement.services.model_silence_guard import (
            install_model_silence_guard,
        )
        install_model_silence_guard()

        # Natural conversation safeguards only. Qualification execution remains
        # owned by the explicit backend contract below.
        from apps.ai_engagement.services.natural_conversation_runtime import (
            install_natural_conversation_runtime,
        )
        install_natural_conversation_runtime()

        # A lead saying they already booked is a report, not a new callback
        # request. Preserve that distinction while still escalating missed calls.
        from apps.ai_engagement.services.natural_conversation_booking_guard import (
            install_natural_conversation_booking_guard,
        )
        install_natural_conversation_booking_guard()

        from apps.ai_engagement.services.engagement_failsoft import (
            install_engagement_failsoft,
        )
        install_engagement_failsoft()

        # Fail-soft is allowed to recover provider/runtime errors, but it must not
        # convert a normal customer turn into silence. Only deterministic opt-out
        # remains a silent EngagementDecision at the service boundary.
        from apps.ai_engagement.services.final_reply_guard import (
            install_final_reply_guard,
        )
        install_final_reply_guard()

        # Final evidence gate for ordinary model-proposed stage moves. Configured
        # deterministic qualification-completion transitions are separately
        # authorized by backend completion state and target-stage configuration.
        from apps.ai_engagement.services.stage_transition_evidence import (
            install_stage_transition_evidence,
        )
        install_stage_transition_evidence()

        # Resolve state-changing actions before the final customer response.
        from apps.ai_engagement.services.transactional_turn_runtime import (
            install_transactional_turn_runtime,
        )
        install_transactional_turn_runtime()

        # State-changing turns use a draft/action pass followed by a fresh final
        # response generated from committed backend state.
        from apps.ai_engagement.services.transactional_decision_reuse import (
            install_transactional_decision_reuse,
        )
        install_transactional_decision_reuse()

        # The second pass is language-only in code, not just by prompt.
        from apps.ai_engagement.services.post_state_finalization_guard import (
            install_post_state_finalization_guard,
        )
        install_post_state_finalization_guard()

        from apps.ai_engagement.services.task_execution_failsoft import (
            install_task_execution_failsoft,
        )
        install_task_execution_failsoft()

        from services.channels.ai_orchestration_hooks import (
            install_ai_orchestration_hooks,
        )
        install_ai_orchestration_hooks()

        # The first customer-facing WhatsApp reply greets once before presenting
        # the backend-selected first qualification requirement.
        from apps.ai_engagement.services.first_inbound_welcome_runtime import (
            install_first_inbound_welcome_runtime,
        )
        install_first_inbound_welcome_runtime()

        # Last-mile customer-chat cleanup remains language-only and may not own
        # qualification state, mappings, or stage transitions.
        from apps.ai_engagement.services.customer_chat_regressions import (
            install_customer_chat_regressions,
        )
        install_customer_chat_regressions()

        # Canonical architecture boundary: configuration -> evidence -> policy ->
        # deterministic engines -> execution -> reconciled state -> validator.
        from apps.ai_engagement.services.canonical_architecture import (
            install_canonical_ai_architecture,
        )
        install_canonical_ai_architecture()

        # Pure policy previews and SimpleTestCase fixtures use non-persistent
        # synthetic lead IDs. Production UUID-backed CRM leads still reconcile.
        from apps.ai_engagement.services.canonical_architecture_compat import (
            install_canonical_architecture_compat,
        )
        install_canonical_architecture_compat()

        # Authoritative qualification execution contract. This owns active-answer
        # resolution, exact configured mapping, completion actions, reconciliation
        # and backend response plans for API and Hosted/Coexistence WhatsApp.
        from apps.ai_engagement.services.qualification_execution_contract import (
            install_qualification_execution_contract,
        )
        install_qualification_execution_contract()

        # Model-interpreted qualification answers must resolve through the same
        # exact configured mapping/completion contract as deterministic answers.
        from apps.ai_engagement.services.qualification_execution_policy_guard import (
            install_qualification_execution_policy_guard,
        )
        install_qualification_execution_policy_guard()

        # Phase 3 conversation strategy is deterministic and transport-neutral.
        # It consumes Phase 2 IntentDecision plus the post-validation qualification
        # state, then constrains the existing engagement generator without becoming
        # a CRM executor or adding a policy-model call.
        from apps.ai_engagement.services.conversation_policy_runtime import (
            install_conversation_policy_runtime,
        )
        install_conversation_policy_runtime()

        # Phase 1 observability is installed last so it observes the final shared
        # API/Coexistence and Hosted runtime without becoming policy authority.
        from . import trace_signals  # noqa: F401
        from apps.ai_engagement.services.ai_trace_runtime import (
            install_ai_trace_runtime,
        )
        install_ai_trace_runtime()
