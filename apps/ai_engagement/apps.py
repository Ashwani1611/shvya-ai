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

        # Install the grounding scope boundary before the qualification routing
        # wrapper is created. This guarantees every wrapper reference, including
        # early imports held by tests/workflows, sees the scoped helper.
        from apps.ai_engagement.services.qualification_grounding_scope_guard import (
            install_qualification_grounding_scope_guard,
        )
        install_qualification_grounding_scope_guard()

        # Active qualification answers are resolved against the persisted current
        # requirement before generic knowledge/grounding fallback. This also
        # normalizes authored option ranges and strengthens description-based
        # attribute mapping without changing organization-specific flows.
        from apps.ai_engagement.services.qualification_answer_routing_runtime import (
            install_qualification_answer_routing_runtime,
        )
        install_qualification_answer_routing_runtime()

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

        # Reply eligibility is backend-owned. The model may generate language,
        # extraction and proposals, but it cannot suppress a valid inbound turn.
        # Explicit opt-out remains deterministic and permission/transport guards
        # continue to run outside the model.
        from apps.ai_engagement.services.model_silence_guard import (
            install_model_silence_guard,
        )
        install_model_silence_guard()

        # Natural conversation safeguards only. Qualification stage scope remains
        # owned by the core New Lead state machine and routing layers above.
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

        # The executor is the final backend evidence gate for non-Qualified stage
        # moves; Qualified remains strictly tied to completed qualification.
        from apps.ai_engagement.services.stage_transition_evidence import (
            install_stage_transition_evidence,
        )
        install_stage_transition_evidence()

        # Reliable existing CRM values can satisfy mapped qualification fields
        # before generation, preventing duplicate questions and stale state.
        from apps.ai_engagement.services.attribute_state_reconciliation import (
            install_attribute_state_reconciliation,
        )
        install_attribute_state_reconciliation()

        # Resolve attributes, qualification, stage and workflow mutations before
        # the canonical task builds the final customer-facing response. Install
        # this before task fail-soft so terminal generation recovery still wraps
        # the complete transactional execution path.
        from apps.ai_engagement.services.transactional_turn_runtime import (
            install_transactional_turn_runtime,
        )
        install_transactional_turn_runtime()

        # State-changing turns use a draft/action pass followed by a fresh final
        # response generated from the committed backend state. The draft response
        # is never reused as customer-facing text after mutations are persisted.
        from apps.ai_engagement.services.transactional_decision_reuse import (
            install_transactional_decision_reuse,
        )
        install_transactional_decision_reuse()

        # The second pass is language-only in code, not just by prompt: strip any
        # repeated CRM/qualification proposals before qualification validation so
        # already-committed actions cannot be repaired, re-applied, or block the
        # final post-state response.
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

        # The first customer-facing WhatsApp reply must always greet once before
        # presenting the backend-selected first qualification requirement.
        from apps.ai_engagement.services.first_inbound_welcome_runtime import (
            install_first_inbound_welcome_runtime,
        )
        install_first_inbound_welcome_runtime()

        # Last-mile regression guard for observed real customer conversations:
        # noun-phrase information requests, short yes/no variants, internal Q-label
        # leakage and whole-response quote wrappers must not break natural chat.
        from apps.ai_engagement.services.customer_chat_regressions import (
            install_customer_chat_regressions,
        )
        install_customer_chat_regressions()

        # Canonical architecture boundary. Install last so legacy compatibility
        # shims feed one explicit contract: configuration -> evidence -> policy ->
        # deterministic engines -> execution -> reconciled state -> final validator.
        from apps.ai_engagement.services.canonical_architecture import (
            install_canonical_ai_architecture,
        )
        install_canonical_ai_architecture()

        # Pure policy previews and SimpleTestCase fixtures use non-persistent
        # synthetic lead IDs. They must remain database-free while production
        # UUID-backed CRM leads continue through reconciliation.
        from apps.ai_engagement.services.canonical_architecture_compat import (
            install_canonical_architecture_compat,
        )
        install_canonical_architecture_compat()

        # Final qualification execution contract. Install after the canonical
        # architecture so answer resolution, exact configured mappings, CRM
        # execution, state reconciliation and response-plan validation share one
        # production path across API and Hosted/Coexistence WhatsApp.
        from apps.ai_engagement.services.qualification_execution_contract import (
            install_qualification_execution_contract,
        )
        install_qualification_execution_contract()

        # Natural-language qualification answers use the same exact mapping and
        # configured completion-stage contract as deterministic option answers.
        from apps.ai_engagement.services.qualification_execution_policy_guard import (
            install_qualification_execution_policy_guard,
        )
        install_qualification_execution_policy_guard()
