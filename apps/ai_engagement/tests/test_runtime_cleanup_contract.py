from __future__ import annotations

import inspect
import re
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement import runtime_bootstrap
from services.channels import hosted_automation_service, whatsapp_service


class RuntimeCleanupContractTests(SimpleTestCase):
    def test_bootstrap_preserves_question_guard_and_personalizes_final_copy_before_trace(self):
        from apps.ai_engagement.graph import workflow
        from apps.ai_engagement.services import first_inbound_welcome_runtime
        from apps.ai_engagement.services.engagement import EngagementDecision, EngagementService
        from apps.ai_engagement.services.qualification_execution.finalization import _finalize
        from apps.organizations.models import Organization

        initial = EngagementDecision(
            should_engage=True,
            message="Generated reply.",
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            model="test",
        )
        organization = Organization(name="SHVYA AI")
        organization._state.adding = False
        traced_messages = []

        def install_finalizer():
            previous = EngagementService.engage

            def finalized(self, **kwargs):
                return _finalize(
                    previous(self, **kwargs),
                    {"response_plan": {
                        "response_type": "qualification_start",
                        "next_requirement": {
                            "id": "q1",
                            "rendered": "{{lead_first_name}}, what is your goal?",
                        },
                    }},
                )

            EngagementService.engage = finalized

        def install_evidence_fallback():
            previous = EngagementService.engage

            def grounded(self, **kwargs):
                decision = previous(self, **kwargs)
                return replace(
                    decision,
                    message="Thanks, {{lead_first_name}}.\n\n" + decision.message,
                )

            EngagementService.engage = grounded

        def install_trace():
            previous = EngagementService.engage

            def traced(self, **kwargs):
                decision = previous(self, **kwargs)
                traced_messages.append(decision.message)
                return decision

            EngagementService.engage = traced

        # Exercise real startup ordering with deterministic backend collaborators;
        # the other installers are already initialized by Django and are no-ops.
        with (
            patch.object(runtime_bootstrap, "_INSTALLED", False),
            patch.object(first_inbound_welcome_runtime, "_INSTALLED", False),
            patch.object(first_inbound_welcome_runtime, "_PERSONALIZATION_INSTALLED", False),
            patch.object(first_inbound_welcome_runtime, "_is_first_inbound_turn", return_value=True),
            patch.object(EngagementService, "engage", lambda self, **kwargs: initial),
            patch("apps.ai_engagement.models.OrgInfo.objects.filter") as info_query,
            patch(
                "apps.ai_engagement.services.canonical_architecture.install_canonical_ai_architecture",
                side_effect=install_finalizer,
            ),
            patch(
                "apps.ai_engagement.services.phase5_6_runtime.install_phase5_6_safety_fixes",
                side_effect=install_evidence_fallback,
            ),
            patch(
                "apps.ai_engagement.services.ai_trace_runtime.install_ai_trace_runtime",
                side_effect=install_trace,
            ),
            patch.object(workflow, "build_engagement_graph", return_value=workflow.ENGAGEMENT_GRAPH),
        ):
            info_query.return_value.only.return_value.first.return_value = SimpleNamespace(
                ai_playbook="## Welcome Message\nHi {{lead_first_name}}! How can I help?",
            )
            runtime_bootstrap.install_ai_runtime()
            result = EngagementService.engage(
                object(),
                organization=organization,
                lead=SimpleNamespace(name="Alex Smith"),
            )

        expected = "Thanks, Alex.\n\nHi Alex!\n\nAlex, what is your goal?"
        self.assertEqual(result.message, expected)
        self.assertEqual(traced_messages, [expected])
        self.assertEqual(result.next_requirement_id, "q1")

    def test_bootstrap_has_no_removed_compatibility_or_channel_hooks(self):
        source = inspect.getsource(runtime_bootstrap.install_ai_runtime)
        for legacy in (
            "canonical_architecture_compat",
            "conditional_state_postfix",
            "pipeline_context_guard",
            "qualification_grounding_scope_guard",
            "natural_conversation_booking_guard",
            "final_reply_guard",
            "ai_setup_runtime_compat",
            "ai_orchestration_hooks",
        ):
            self.assertNotIn(legacy, source)

    @patch("apps.ai_engagement.services.execution_tracker.queue_api_engagement")
    def test_api_queue_uses_durable_execution_tracker_directly(self, queue):
        queue.return_value = {"status": "queued"}

        result = whatsapp_service._queue_whatsapp_engagement(lead_id="lead-123")

        queue.assert_called_once_with(lead_id="lead-123")
        self.assertEqual(result, {"status": "queued"})

    def test_summary_queue_is_signal_owned(self):
        self.assertIsNone(
            whatsapp_service._queue_internal_conversation_summary(
                lead_id="lead-123"
            )
        )

    def test_hosted_ai_delay_is_bounded_without_startup_patch(self):
        self.assertEqual(hosted_automation_service.AI_RESPONSE_DELAY_SECONDS, 45)


    def test_bootstrap_installer_set_is_explicit_and_cannot_grow_silently(self):
        source = inspect.getsource(runtime_bootstrap.install_ai_runtime)
        installers = set(
            re.findall(r"\b(install_[a-z0-9_]+)\(\)", source)
        )
        installers.discard("install_ai_runtime")
        self.assertEqual(
            installers,
            {
                "install_conditional_qualification_runtime",
                "install_fixed_prompt_overrides",
                "install_ai_setup_runtime_fixes",
                "install_conversation_priority_runtime",
                "install_crm_routing_reliability",
                "install_engagement_instruction_runtime",
                "install_qualification_answer_routing_runtime",
                "install_langgraph_orchestration",
                "install_model_silence_guard",
                "install_natural_conversation_runtime",
                "install_engagement_failsoft",
                "install_stage_transition_evidence",
                "install_transactional_turn_runtime",
                "install_transactional_decision_reuse",
                "install_post_state_finalization_guard",
                "install_task_execution_failsoft",
                "install_first_inbound_welcome_runtime",
                "install_first_name_personalization_runtime",
                "install_customer_chat_regressions",
                "install_canonical_ai_architecture",
                "install_qualification_execution_contract",
                "install_qualification_execution_policy_guard",
                "install_phase4_runtime",
                "install_conversation_policy_runtime",
                "install_phase5_6_runtime",
                "install_phase5_6_safety_fixes",
                # Reviewed read-only continuity hooks; keep the allow-list exact.
                "install_conversation_continuity_runtime",
                "install_ai_trace_runtime",
                # Reviewed final text translation; no action or state ownership.
                "install_final_reply_language",
            },
        )

    def test_continuity_installs_before_graph_compile_and_trace_once(self):
        from apps.ai_engagement.graph import workflow
        from apps.ai_engagement.services import ai_trace_runtime, conversation_continuity_runtime

        order = []
        compiled_graph = workflow.ENGAGEMENT_GRAPH

        def compile_graph():
            order.append("compile")
            return compiled_graph

        # Execute the bootstrap itself; previously installed collaborators are
        # no-ops. Resetting the outer guard must not change installer order.
        with (
            patch.object(runtime_bootstrap, "_INSTALLED", False),
            patch.object(
                conversation_continuity_runtime,
                "install_conversation_continuity_runtime",
                side_effect=lambda: order.append("continuity"),
            ) as install_continuity,
            patch.object(workflow, "build_engagement_graph", side_effect=compile_graph) as compile_mock,
            patch.object(workflow, "ENGAGEMENT_GRAPH", compiled_graph),
            patch.object(
                ai_trace_runtime,
                "install_ai_trace_runtime",
                side_effect=lambda: order.append("trace"),
            ) as install_trace,
        ):
            runtime_bootstrap.install_ai_runtime()
            runtime_bootstrap.install_ai_runtime()
            self.assertEqual(order, ["continuity", "compile", "trace"])
            install_continuity.assert_called_once_with()
            compile_mock.assert_called_once_with()
            install_trace.assert_called_once_with()

    def test_continuity_repeated_install_does_not_stack_wrappers(self):
        from apps.ai_engagement.graph import workflow
        from apps.ai_engagement.services import conversation_continuity_runtime
        from apps.ai_engagement.services.engagement import EngagementService
        from apps.ai_engagement.services.file_sharing import FileSharingService

        targets = [
            (EngagementService, "_build_input"),
            (EngagementService, "_build_instructions"),
            (EngagementService, "_should_retrieve_knowledge"),
            (EngagementService, "_build_knowledge_query"),
            (EngagementService, "_validate_engagement_policy"),
            (FileSharingService, "build_file_candidates"),
            (workflow, "_deterministic_extract"),
            (workflow, "_route_turn"),
        ]
        with ExitStack() as stack:
            stack.enter_context(patch.object(conversation_continuity_runtime, "_INSTALLED", False))
            originals = []
            for owner, name in targets:
                original = getattr(owner, name)
                originals.append(original)
                stack.enter_context(patch.object(owner, name, original))

            conversation_continuity_runtime.install_conversation_continuity_runtime()
            installed = [getattr(owner, name) for owner, name in targets]
            for original, wrapped in zip(originals, installed, strict=True):
                self.assertIsNot(original, wrapped)
                self.assertIs(wrapped.__wrapped__, original)

            conversation_continuity_runtime.install_conversation_continuity_runtime()
            for (owner, name), wrapped in zip(targets, installed, strict=True):
                self.assertIs(getattr(owner, name), wrapped)
