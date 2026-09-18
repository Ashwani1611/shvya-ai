from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.canonical_architecture import ResponseActionValidator
from apps.ai_engagement.services.crm_executor import (
    CRMActionExecutionError,
    CRMActionExecutor,
)
from apps.ai_engagement.services.engagement import (
    EngagementDecision,
    EngagementError,
)
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_execution_contract import (
    _ack_from_message,
    _config,
    resolve_before_generation,
)
from apps.ai_engagement.services.qualification_state import (
    record_last_asked_requirement,
    state_for_lead,
)
from apps.ai_engagement.services import transactional_turn_runtime
from apps.ai_engagement.tests.test_engagement_controls import (
    AIEngagementControlTests,
)
from apps.channels.models import WhatsAppMessage
from apps.crm.models import AttributeDefinition, LeadReminder, Stage


class _NoRetryTask:
    def retry(self, **kwargs):  # pragma: no cover - any retry is a test failure
        raise AssertionError(f"unexpected Celery retry: {kwargs}")


class QualificationExecutionContractE2ETests(TestCase):
    """Exercise the same task/runtime boundaries used by production WhatsApp."""

    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def _create_target_stage(self, name: str):
        order = max(self.pipeline.stages.values_list("display_order", flat=True)) + 10
        return Stage.objects.create(
            pipeline=self.pipeline,
            name=name,
            display_order=order,
            is_active=True,
            ai_on=True,
        )

    def _configure_two_step_org(self):
        target = self._create_target_stage("Ready for Consultation")
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Acquisition Route",
            key="acquisition_route",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Sales Motion",
            key="sales_motion",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: acquisition] Where do most enquiries originate?\n"
            "A. Search\n"
            "B. Partner referrals\n"
            "C. Events\n"
            "[id: motion] How are new enquiries handled today?\n"
            "A. Dedicated sales team\n"
            "B. Founder-led\n"
            "C. Shared inbox\n"
            "All questions are required"
        )
        info.engagement_instructions = (
            "Be concise, specific, and natural.\n\n"
            "## Attribute mapped\n"
            "acquisition -> Acquisition Route\n"
            "motion -> Sales Motion\n\n"
            "## Stage shifting\n"
            "When all required qualification questions are answered, move to "
            "Ready for Consultation.\n"
            "Acknowledgment message: \"Your details are complete and our team can take the next step.\""
        )
        info.bot_languages = "English"
        info.ai_enabled = True
        info.save()
        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        return target, requirements

    def _source(self, external_id: str, body: str):
        inbound = self._inbound(external_id)
        inbound.body = body
        inbound.save(update_fields=["body"])
        return inbound

    def _reconciled(self, inbound):
        inbound.refresh_from_db()
        return inbound.raw_payload["shvya_ai_processing"]["reconciled_state"]

    def _decision(self, message: str, *, next_requirement_id=None):
        return EngagementDecision(
            should_engage=True,
            message=message,
            file_document_id=None,
            crm_actions=[],
            qualification_updates=[],
            next_requirement_id=next_requirement_id,
            reason=("QUALIFICATION_NEXT" if next_requirement_id else "NORMAL_CONVERSATION"),
            reason_code=("QUALIFICATION_NEXT" if next_requirement_id else "NORMAL_CONVERSATION"),
            model="test",
        )

    def test_acknowledgement_sanitizer_removes_model_paraphrased_question(self):
        plan = {
            "next_requirement": {
                "question": "Do you currently run ads?",
                "rendered": "Do you currently run ads?\nA. Yes\nB. No",
                "options": [
                    {"key": "A", "value": "Yes"},
                    {"key": "B", "value": "No"},
                ],
            },
            "final_configured_acknowledgement": {"value": ""},
        }
        acknowledgement = _ack_from_message(
            "Thanks, that helps. Are you currently running ads?\\nA. Yes\\nB. No",
            plan,
        )
        self.assertEqual(acknowledgement, "Thanks, that helps.")

    def test_config_reads_mapping_and_multiline_ack_from_qualification_field(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Biggest Problem",
            key="biggest_problem",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "Q1. What is your biggest challenge?\n"
            "A. Slow replies\n"
            "B. Missed follow-ups\n"
            "Acknowledgment Message:\n"
            "\"Thanks for sharing the details. Our team will connect with you shortly.\"\n"
            "ATTRIBUTE MAPPING\n"
            "Q1 -> Biggest Problem\n"
            "RULES\n"
            "- Ask only the next unanswered question."
        )
        info.engagement_instructions = "Be concise and natural."
        info.save()

        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        self.assertEqual(len(requirements), 1)

        config = _config(
            organization=self.organization,
            requirements=requirements,
        )
        self.assertEqual(
            config["mappings"][requirements[0]["id"]],
            "biggest_problem",
        )
        self.assertEqual(
            config["final_ack"],
            "Thanks for sharing the details. Our team will connect with you shortly.",
        )

    def test_multi_target_mapping_shorthand_resolves_exact_attribute_names(self):
        for name, key in (
            ("Lead Management Tool", "lead_management_tool"),
            ("Using Whatsapp", "using_whatsapp"),
            ("CRM", "crm"),
            ("Leads/d", "leads_d"),
        ):
            AttributeDefinition.objects.create(
                organization=self.organization,
                name=name,
                key=key,
                field_type=AttributeDefinition.FieldType.TEXT,
            )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: management] Where do you currently manage your leads?\n"
            "A. WhatsApp chats\n"
            "B. CRM\n"
            "[id: volume] How many leads do you receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "management -> Lead Management Tool (+ Using Whatsapp / CRM)\n"
            "volume -> Leads/d\n"
        )
        info.save()
        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]

        config = _config(
            organization=self.organization,
            requirements=requirements,
        )

        self.assertEqual(
            config["mapping_targets"][requirements[0]["id"]],
            ["lead_management_tool", "using_whatsapp", "crm"],
        )
        self.assertEqual(
            config["mapping_targets"][requirements[1]["id"]],
            ["leads_d"],
        )
        self.assertFalse(any(
            item.get("code") == "unknown_attribute_mapping_reference"
            for item in config["errors"]
        ))

    def test_one_answer_can_fill_multiple_explicitly_mapped_attributes_and_fallback_to_qualified(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead Management Tool",
            key="lead_management_tool",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Using Whatsapp",
            key="using_whatsapp",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: management] Where do you currently manage your leads?\n"
            "A. WhatsApp chats\n"
            "B. Excel / Sheets\n"
            "C. CRM\n"
            "D. Multiple places\n"
            "All questions are required\n"
            "Acknowledgment message: \"Thanks for sharing the details. Our team will connect with you shortly.\""
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "management -> Lead Management Tool\n"
            "management -> Using Whatsapp\n"
        )
        info.ai_enabled = True
        info.save()

        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        source = self._source("multi-map-final", "Whatsapp")

        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=source.pk,
        )

        self.assertTrue(result["applied"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["lead_management_tool"], "WhatsApp chats")
        self.assertEqual(self.lead.attributes["using_whatsapp"], "WhatsApp chats")
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        config = _config(
            organization=self.organization,
            requirements=requirements,
        )
        self.assertEqual(
            config["mapping_targets"][requirements[0]["id"]],
            ["lead_management_tool", "using_whatsapp"],
        )

    def test_completed_pre_fix_lead_self_heals_missing_attributes_stage_and_reminder(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead Management Tool",
            key="lead_management_tool",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Using Whatsapp",
            key="using_whatsapp",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: management] Where do you currently manage your leads?\n"
            "A. WhatsApp chats\n"
            "B. CRM\n"
            "All questions are required\n"
            "Acknowledgment message: \"Thanks for sharing the details. Our team will connect with you shortly.\""
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "management -> Lead Management Tool\n"
            "management -> Using Whatsapp\n\n"
            "## Reminders\n"
            "When qualification is completed, create a reminder after 1 day.\n"
        )
        info.ai_enabled = True
        info.save()

        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        original = self._source("pre-fix-complete", "A")
        completed = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=original.pk,
        )
        self.assertTrue(completed["applied"])

        # Simulate the exact legacy production damage: qualification state was
        # completed, but one mapped field, the completion-stage move, and the
        # reminder were missing. Preserve a non-empty human-edited value.
        self.lead.refresh_from_db()
        attrs = dict(self.lead.attributes or {})
        attrs["lead_management_tool"] = "Human override"
        attrs.pop("using_whatsapp", None)
        self.lead.attributes = attrs
        self.lead.stage = self.new_lead
        self.lead.save(update_fields=["attributes", "stage", "updated_at"])
        LeadReminder.objects.filter(lead=self.lead).delete()

        later = self._source("completed-repair-turn", "Thanks")
        repaired = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=later.pk,
        )

        self.assertTrue(repaired["applied"])
        self.assertEqual(
            repaired["reason"],
            "qualification_completion_reconciled",
        )
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes["lead_management_tool"],
            "Human override",
        )
        self.assertEqual(
            self.lead.attributes["using_whatsapp"],
            "WhatsApp chats",
        )
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        self.assertEqual(
            LeadReminder.objects.filter(
                lead=self.lead,
                title="Follow up with qualified lead",
            ).count(),
            1,
        )

        # A later inbound turn must not duplicate the completion reminder.
        again = self._source("completed-repair-second-turn", "Okay")
        second = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=again.pk,
        )
        self.assertFalse(second["applied"])
        self.assertEqual(second["reason"], "qualification_already_complete")
        self.assertEqual(
            LeadReminder.objects.filter(
                lead=self.lead,
                title="Follow up with qualified lead",
            ).count(),
            1,
        )

    def test_completion_reminder_runs_only_from_explicit_configured_rule(self):
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead Source Answer",
            key="lead_source_answer",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: source] Where do most of your leads currently come from?\n"
            "A. Referrals\n"
            "B. Organic search\n"
            "All questions are required\n"
            "Acknowledgment message: \"Thanks for sharing the details. Our team will connect with you shortly.\""
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "source -> Lead Source Answer\n\n"
            "## Reminders\n"
            "When qualification is completed, create a reminder after 1 day.\n"
        )
        info.ai_enabled = True
        info.save()

        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        source = self._source("completion-reminder", "A")

        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=source.pk,
        )

        self.assertTrue(result["applied"])
        reminder = LeadReminder.objects.get(lead=self.lead)
        self.assertEqual(reminder.status, "pending")
        self.assertGreater(reminder.due_at, source.created_at)
        self.assertTrue(any(
            item.get("type") == "create_reminder"
            and item.get("status") == "executed"
            for item in result["execution_results"]
        ))

    def test_non_final_answer_persists_exact_mapping_and_builds_progress_response(self):
        target, requirements = self._configure_two_step_org()
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        inbound = self._source("contract-progress", "B")

        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.pk,
        )
        self.assertTrue(result["applied"])

        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.attributes["acquisition_route"], "Partner referrals")
        self.assertNotIn("sales_motion", self.lead.attributes)
        self.assertEqual(state["qualification_status"], "in_progress")
        self.assertEqual(self.lead.stage_id, self.new_lead.id)
        self.assertNotEqual(self.lead.stage_id, target.id)

        plan = result["response_plan"]
        self.assertEqual(plan["response_type"], "qualification_progress")
        self.assertTrue(plan["acknowledgement_required"])
        self.assertEqual(plan["next_requirement"]["id"], requirements[1]["id"])
        self.assertEqual(
            plan["next_requirement"]["options"],
            requirements[1]["options"],
        )

        reconciled = self._reconciled(inbound)
        final = ResponseActionValidator().validate(
            decision=self._decision(
                "Partner referrals are your main route, which gives us useful context.",
                next_requirement_id=requirements[1]["id"],
            ),
            reconciled_state=reconciled,
        )
        self.assertIn("Partner referrals", final.message)
        self.assertIn("How are new enquiries handled today?", final.message)
        for option in requirements[1]["options"]:
            self.assertIn(
                f"{option['key']}. {option['value']}",
                final.message,
            )

    def test_response_plan_sets_metadata_for_the_question_it_actually_renders(self):
        _target, requirements = self._configure_two_step_org()
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        inbound = self._source("contract-metadata-progress", "B")
        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=inbound.pk,
        )
        self.assertTrue(result["applied"])

        reconciled = self._reconciled(inbound)
        final = ResponseActionValidator().validate(
            decision=self._decision(
                "Partner referrals are your main route, which gives us useful context.",
                next_requirement_id=None,
            ),
            reconciled_state=reconciled,
        )

        self.assertEqual(final.next_requirement_id, requirements[1]["id"])
        self.assertEqual(final.reason_code, "QUALIFICATION_NEXT")
        self.assertIn("How are new enquiries handled today?", final.message)

    def test_final_answer_persists_all_mappings_moves_configured_stage_and_uses_ack_value(self):
        target, requirements = self._configure_two_step_org()
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        first = self._source("contract-first", "B")
        first_result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=first.pk,
        )
        self.assertTrue(first_result["applied"])

        record_last_asked_requirement(
            self.lead,
            requirements[1]["id"],
            requirements=requirements,
        )
        final_inbound = self._source("contract-final", "A")
        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=final_inbound.pk,
        )
        self.assertTrue(result["applied"])

        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.attributes["acquisition_route"], "Partner referrals")
        self.assertEqual(self.lead.attributes["sales_motion"], "Dedicated sales team")
        self.assertEqual(state["qualification_status"], "completed")
        self.assertEqual(self.lead.stage_id, target.id)

        plan = result["response_plan"]
        self.assertEqual(plan["response_type"], "qualification_complete")
        self.assertEqual(
            plan["final_configured_acknowledgement"]["value"],
            "Your details are complete and our team can take the next step.",
        )
        stage_result = plan["execution_results"]["stage_transition"]["result"]
        self.assertTrue(stage_result["verified"])
        self.assertEqual(stage_result["actual_stage_id"], str(target.id))

        reconciled = self._reconciled(final_inbound)
        final = ResponseActionValidator().validate(
            decision=self._decision(
                "A dedicated sales team gives us a clear picture of your current setup."
            ),
            reconciled_state=reconciled,
        )
        self.assertEqual(
            final.message,
            "A dedicated sales team gives us a clear picture of your current setup.\n\n"
            "Your details are complete and our team can take the next step.",
        )
        self.assertNotIn("Acknowledgment message", final.message)
        self.assertNotIn("Completion Message", final.message)
        self.assertNotIn(target.name, final.message)

    def test_production_task_path_final_answer_updates_db_then_queues_reconciled_reply(self):
        target, requirements = self._configure_two_step_org()
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        first = self._source("task-first", "C")
        resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=first.pk,
        )
        record_last_asked_requirement(
            self.lead,
            requirements[1]["id"],
            requirements=requirements,
        )
        source = self._source("task-final", "B")

        provider_payload = {
            "should_engage": True,
            "silence_rule": None,
            "message": "Founder-led handling tells us how enquiries are being managed today.",
            "file_document_id": None,
            "crm_actions": [],
            "qualification_updates": [],
            "next_requirement_id": None,
            "reason_code": "NORMAL_CONVERSATION",
        }
        with patch(
            "apps.ai_engagement.services.engagement.OpenAIProvider"
        ) as provider_cls:
            provider_cls.return_value.generate_text.return_value = AITextResult(
                json.dumps(provider_payload),
                "test-model",
            )
            result = __import__(
                "apps.ai_engagement.tasks",
                fromlist=["_execute_ai_engagement_response_impl"],
            )._execute_ai_engagement_response_impl(
                task=_NoRetryTask(),
                lead_id=str(self.lead.pk),
            )

        self.assertEqual(result["status"], "completed")
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.attributes["acquisition_route"], "Events")
        self.assertEqual(self.lead.attributes["sales_motion"], "Founder-led")
        self.assertEqual(state["qualification_status"], "completed")
        self.assertEqual(self.lead.stage_id, target.id)

        outbound = WhatsAppMessage.objects.get(
            organization=self.organization,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            raw_payload__shvya_ai__source_inbound_message_id=str(source.id),
        )
        self.assertIn("Founder-led handling", outbound.body)
        self.assertIn(
            "Your details are complete and our team can take the next step.",
            outbound.body,
        )
        self.assertNotIn("Acknowledgment message", outbound.body)
        self.assertNotIn(target.name, outbound.body)

        with patch(
            "apps.ai_engagement.services.engagement.OpenAIProvider"
        ) as provider_cls:
            provider_cls.return_value.generate_text.return_value = AITextResult(
                json.dumps(provider_payload),
                "test-model",
            )
            second = __import__(
                "apps.ai_engagement.tasks",
                fromlist=["_execute_ai_engagement_response_impl"],
            )._execute_ai_engagement_response_impl(
                task=_NoRetryTask(),
                lead_id=str(self.lead.pk),
            )
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(
            WhatsAppMessage.objects.filter(
                organization=self.organization,
                lead=self.lead,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                raw_payload__shvya_ai__source_inbound_message_id=str(source.id),
            ).count(),
            1,
        )

    def test_invalid_explicit_mapping_never_falls_back_to_semantic_guessing(self):
        target = self._create_target_stage("Review Queue")
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Primary Source",
            key="primary_source",
            description="Where enquiries come from.",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: origin] Where do enquiries come from?\n"
            "A. Community\n"
            "B. Search\n"
            "All questions are required"
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "origin -> Missing Attribute\n\n"
            "## Stage shifting\n"
            "When all required qualification questions are answered, move to Review Queue.\n"
            "Acknowledgment message: \"We have what we need.\""
        )
        info.save()
        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        source = self._source("bad-map", "A")

        result = resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=source.pk,
        )
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(state["qualification_status"], "completed")
        self.assertNotIn("primary_source", self.lead.attributes)
        # Attribute persistence and configured stage execution are independent
        # results. A bad mapping must not silently choose another attribute, but
        # it also must not cancel a valid deterministic completion-stage action.
        self.assertEqual(self.lead.stage_id, target.id)
        self.assertTrue(any(
            item.get("code") == "unknown_attribute_mapping_reference"
            for item in result["execution_results"]
        ))
        stage_result = result["response_plan"]["execution_results"]["stage_transition"]["result"]
        self.assertTrue(stage_result["verified"])
        self.assertEqual(stage_result["actual_stage_id"], str(target.id))

    def test_failed_stage_execution_is_reconciled_and_never_claimed_as_success(self):
        target = self._create_target_stage("Advisor Review")
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Fit Signal",
            key="fit_signal",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: fit] Which route fits you best?\n"
            "A. Guided setup\n"
            "B. Self service\n"
            "All questions are required"
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "fit -> Fit Signal\n\n"
            "## Stage shifting\n"
            "When all required qualification questions are answered, move to Advisor Review.\n"
            "Acknowledgment message: \"Your answers have been recorded.\""
        )
        info.save()
        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        source = self._source("stage-fail", "A")

        original_execute = CRMActionExecutor.execute

        def execute_with_stage_failure(executor, *, organization, lead, actions, actor=None):
            if any(action.get("type") == "pipeline_transition" for action in actions):
                raise CRMActionExecutionError("simulated stage failure")
            return original_execute(
                executor,
                organization=organization,
                lead=lead,
                actions=actions,
                actor=actor,
            )

        with patch.object(
            CRMActionExecutor,
            "execute",
            new=execute_with_stage_failure,
        ):
            result = resolve_before_generation(
                organization=self.organization,
                lead=self.lead,
                source_message_id=source.pk,
            )

        self.lead.refresh_from_db()
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(state["qualification_status"], "completed")
        self.assertEqual(self.lead.attributes["fit_signal"], "Guided setup")
        self.assertEqual(self.lead.stage_id, self.new_lead.id)
        self.assertNotEqual(self.lead.stage_id, target.id)
        stage_result = result["response_plan"]["execution_results"]["stage_transition"]["result"]
        self.assertEqual(stage_result["status"], "failed")

        reconciled = self._reconciled(source)
        final = ResponseActionValidator().validate(
            decision=self._decision(
                "Guided setup is a clear preference. I've moved you to Advisor Review."
            ),
            reconciled_state=reconciled,
        )
        self.assertIn("Guided setup is a clear preference.", final.message)
        self.assertNotIn("Advisor Review", final.message)
        self.assertIn("Your answers have been recorded.", final.message)

    def test_model_interpreted_answer_uses_exact_configured_mapping_not_fuzzy_action(self):
        target = self._create_target_stage("Discovery Complete")
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Decision Window",
            key="decision_window",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Timeline Mirror",
            key="timeline_mirror",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Timeline Guess",
            key="timeline_guess",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.qualification_requirements = (
            "[id: timing] When would you like to get started?\n"
            "All questions are required"
        )
        info.engagement_instructions = (
            "## Attribute mapped\n"
            "timing -> Decision Window\n"
            "timing -> Timeline Mirror\n\n"
            "## Stage shifting\n"
            "When all required qualification questions are answered, move to Discovery Complete.\n"
            "Acknowledgment message: \"We have captured your requirements.\""
        )
        info.save()
        requirements = compile_qualification_requirements(
            info.qualification_requirements
        )["requirements"]
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        source = self._source("model-answer", "Sometime after the festival season")
        decision = EngagementDecision(
            should_engage=True,
            message="That timing gives us useful planning context.",
            file_document_id=None,
            crm_actions=[
                {
                    "type": "attribute_updates",
                    "updates": [
                        {
                            "key": "timeline_guess",
                            "value": "Sometime after the festival season",
                        }
                    ],
                }
            ],
            qualification_updates=[
                {
                    "requirement_id": requirements[0]["id"],
                    "value": "Sometime after the festival season",
                    "source_message_id": str(source.id),
                    "evidence": "Sometime after the festival season",
                }
            ],
            next_requirement_id=None,
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            model="test",
        )

        result = transactional_turn_runtime._resolve_state_before_response(
            organization=self.organization,
            lead=self.lead,
            source_message_id=source.id,
            decision=decision,
        )
        self.assertTrue(result["applied"])
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes["decision_window"],
            "Sometime after the festival season",
        )
        self.assertEqual(
            self.lead.attributes["timeline_mirror"],
            "Sometime after the festival season",
        )
        self.assertNotIn("timeline_guess", self.lead.attributes)
        self.assertEqual(self.lead.stage_id, target.id)

    def test_internal_completion_label_is_rejected_instead_of_sent(self):
        _target, requirements = self._configure_two_step_org()
        record_last_asked_requirement(
            self.lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        first = self._source("label-first", "A")
        resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=first.id,
        )
        record_last_asked_requirement(
            self.lead,
            requirements[1]["id"],
            requirements=requirements,
        )
        source = self._source("label-final", "C")
        resolve_before_generation(
            organization=self.organization,
            lead=self.lead,
            source_message_id=source.id,
        )
        reconciled = self._reconciled(source)

        with self.assertRaises(EngagementError):
            ResponseActionValidator().validate(
                decision=self._decision("Completion Message"),
                reconciled_state=reconciled,
            )
