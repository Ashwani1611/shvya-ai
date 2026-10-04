"""Persisted Instagram qualification actions match the shared CRM contract."""
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.crm_executor import CRMActionExecutionError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import (
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.channels.tests import test_instagram_ai as fixtures
from apps.crm.models import AttributeDefinition, LeadReminder
from services.channels.instagram_ai import _apply_decision_state, execute_instagram_ai_engagement
from tests.playbook_fixtures import build_ai_playbook, qualification_questions, with_playbook_section


class InstagramQualificationActionTests(TestCase):
    setUp = fixtures.InstagramAIEngagementTests.setUp
    task = fixtures.InstagramAIEngagementTests.task

    def configure(self):
        AttributeDefinition.objects.create(
            organization=self.org, name="Acquisition Route", key="acquisition_route",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        AttributeDefinition.objects.create(
            organization=self.org, name="Sales Motion", key="sales_motion",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.org)
        info.ai_playbook = build_ai_playbook(
            questions="[id: acquisition] Where do most enquiries originate?\nA. Search\nB. Partner referrals\n"
            "[id: motion] How are enquiries handled?\nA. Dedicated sales team\nB. Founder-led\nAll questions are required",
            rules="## Attribute mapped\nacquisition -> Acquisition Route\nmotion -> Sales Motion\n\n"
            "## Stage shifting\nWhen all required qualification questions are answered, move to Qualified.\n\n"
            "## Reminder creation logic\nAfter all qualification questions are answered, create a reminder in 1 day.",
        )
        info.save()
        return compile_qualification_requirements(qualification_questions(info.ai_playbook))["requirements"]

    def decision(self, updates=(), actions=()):
        return EngagementDecision(
            should_engage=True, message="Thanks, I have your details.", file_document_id=None,
            crm_actions=list(actions), qualification_updates=list(updates),
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test",
        )

    def test_model_answers_fill_authored_attributes_and_complete_phone_optional_lead(self):
        requirements = self.configure()
        self.inbound.body = "Partner referrals; Founder-led"
        self.inbound.save(update_fields=["body"])
        updates = [
            {"requirement_id": requirement["id"], "value": value,
             "source_message_id": str(self.inbound.pk), "evidence": value}
            for requirement, value in zip(requirements, ("Partner referrals", "Founder-led"), strict=True)
        ]
        result = _apply_decision_state(
            organization=self.org, lead=self.lead, source=self.inbound,
            decision=self.decision(updates), finalize=False,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.phone, "")
        self.assertEqual(self.lead.attributes["acquisition_route"], "Partner referrals")
        self.assertEqual(self.lead.attributes["sales_motion"], "Founder-led")
        self.assertEqual(self.lead.stage.name.casefold(), "qualified")
        self.assertTrue(state_for_lead(self.lead, requirements=requirements)["qualification_completed"])
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead, lead__organization=self.org).count(), 1)
        self.assertEqual({item["type"] for item in result}, {"attribute_updates", "pipeline_transition", "create_reminder"})
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead, organization=self.org).count(), 3)
        _apply_decision_state(
            organization=self.org, lead=self.lead, source=self.inbound,
            decision=self.decision(updates), finalize=False,
        )
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead, lead__organization=self.org).count(), 1)
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead, organization=self.org).count(), 3)

    def test_model_cannot_override_the_exact_authored_attribute_destination(self):
        requirements = self.configure()
        self.inbound.body = "Partner referrals"
        self.inbound.save(update_fields=["body"])
        update = {"requirement_id": requirements[0]["id"], "value": "Partner referrals",
                  "source_message_id": str(self.inbound.pk), "evidence": self.inbound.body}
        _apply_decision_state(
            organization=self.org, lead=self.lead, source=self.inbound,
            decision=self.decision([update], [{"type": "attribute_updates", "updates": [
                {"key": "acquisition_route", "value": "Search"},
                {"key": "sales_motion", "value": "Partner referrals"},
            ]}]), finalize=False,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["acquisition_route"], "Partner referrals")
        self.assertNotIn("sales_motion", self.lead.attributes)
        self.assertEqual(self.lead.stage_id, self.stage.pk)

    def test_invalid_source_evidence_cannot_mutate_mapped_attributes(self):
        requirements = self.configure()
        update = {"requirement_id": requirements[0]["id"], "value": "Search",
                  "source_message_id": str(self.inbound.pk), "evidence": "Search"}
        with self.assertRaises(CRMActionExecutionError):
            _apply_decision_state(
                organization=self.org, lead=self.lead, source=self.inbound,
                decision=self.decision([update]), finalize=False,
            )
        self.lead.refresh_from_db()
        self.assertNotIn("acquisition_route", self.lead.attributes)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead, organization=self.org).exists())

    def test_dated_customer_fact_does_not_authorize_a_model_reminder(self):
        from datetime import timedelta
        from django.utils import timezone

        self.inbound.body = "Our annual contract renews tomorrow at 3 PM."
        self.inbound.save(update_fields=["body"])
        _apply_decision_state(
            organization=self.org, lead=self.lead, source=self.inbound,
            decision=self.decision(actions=[{
                "type": "create_reminder", "title": "Call lead", "description": "Call about renewal.",
                "due_at": (timezone.now() + timedelta(days=1)).isoformat(),
            }]), finalize=False,
        )
        self.assertFalse(LeadReminder.objects.filter(lead=self.lead, lead__organization=self.org).exists())
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead, organization=self.org).exists())

    def test_qualification_answer_and_callback_use_authored_title_without_model_reminder(self):
        from zoneinfo import ZoneInfo
        from django.utils import timezone

        requirements = self.configure()
        self.org.timezone = "Asia/Kolkata"
        self.org.save(update_fields=["timezone"])
        info = OrgInfo.objects.get(organization=self.org)
        info.ai_playbook = with_playbook_section(
            info.ai_playbook, "Reminder creation logic",
            "Reminder 1:\n"
            "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
            "- Title: Customer Callback.",
        )
        info.save(update_fields=["ai_playbook"])
        self.inbound.body = "Partner referrals. Please call me tomorrow at 3 PM."
        self.inbound.save(update_fields=["body"])
        update = {"requirement_id": requirements[0]["id"], "value": "Partner referrals",
                  "source_message_id": str(self.inbound.pk), "evidence": "Partner referrals"}
        result = _apply_decision_state(
            organization=self.org, lead=self.lead, source=self.inbound,
            decision=self.decision([update]), finalize=False,
        )
        reminder = LeadReminder.objects.get(lead=self.lead, lead__organization=self.org)
        self.assertEqual(reminder.title, "Customer Callback")
        self.assertEqual(timezone.localtime(reminder.due_at, ZoneInfo("Asia/Kolkata")).hour, 15)
        self.assertGreater(reminder.due_at, timezone.now())
        self.assertEqual({item["type"] for item in result}, {"attribute_updates", "create_reminder"})
        self.assertEqual(self.lead.stage_id, self.stage.pk)

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_already_captured_option_still_commits_mapping_before_final_reply(self, engage, allowed, dispatch):
        requirements = self.configure()
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        self.inbound.body = "B"
        self.inbound.save(update_fields=["body"])
        phases = []

        def generate(**kwargs):
            from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY

            final = _FINAL_LANGUAGE_ONLY.get()
            phases.append(final)
            if final:
                kwargs["lead"].refresh_from_db()
                self.assertEqual(kwargs["lead"].attributes["acquisition_route"], "Partner referrals")
            else:
                apply_unambiguous_reply(
                    lead=kwargs["lead"], requirements=requirements, text="B",
                    source_message_id=str(self.inbound.pk),
                )
            from dataclasses import replace
            from apps.ai_engagement.services.runtime_state import state_revision

            return replace(self.decision(), backend_revision=state_revision(kwargs["lead"]))

        engage.side_effect = generate
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(phases, [False, True])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["acquisition_route"], "Partner referrals")
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead, action_type="UPDATE_ATTRIBUTE").count(), 1)
        dispatch.assert_called_once()
