"""Authored mappings, completion routing and channel evidence regressions."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import qualification_questions
from apps.ai_engagement.services.qualification_execution.common import _split_mapping
from apps.ai_engagement.services.qualification_execution.config import _config
from apps.ai_engagement.services.qualification_execution.completion import _completion_target
from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.crm.models import AttributeDefinition, Pipeline, Stage


class PlaybookMappingAndReminderTests(SimpleTestCase):
    def test_numbered_and_question_alias_mapping_forms(self):
        self.assertEqual(_split_mapping("1. Q1 -> Budget"), ("Q1", "Budget"))
        for source in ("Q1", "Question 1", "Qualification Question 1"):
            with self.subTest(source=source):
                self.assertEqual(
                    _split_mapping("Mapping 1:\n- Attribute name: Budget\n- Source: " + source),
                    ("Q1", "Budget"),
                )

    def test_explicit_relative_days_and_weeks_are_grounded(self):
        now = datetime(2026, 10, 2, 10, tzinfo=datetime_timezone.utc)
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=now):
            for text, delta in (
                ("Call me after 2 days", timedelta(days=2)),
                ("Remind me in 1 week", timedelta(weeks=1)),
                ("Call after 30 minutes", timedelta(minutes=30)),
            ):
                with self.subTest(text=text):
                    self.assertEqual(datetime.fromisoformat(parse_grounded_due_at(text)), now + delta)
            self.assertIsNone(parse_grounded_due_at("Call me in 0 days"))
            self.assertIsNone(parse_grounded_due_at("Call me sometime next week"))


class PlaybookActionRegressionTests(TestCase):
    setUp = AIEngagementControlTests.setUp

    def _playbook(self, *, mapping="", routing=""):
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = (
            "## Qualification Questions\nWhat is your budget?\n"
            "## Qualification Criteria\nAll required questions are answered.\n"
            "## Attribute mapping logic\n" + mapping + "\n"
            "## Stage shifting logic\n" + routing
        )
        info.save()
        requirements = compile_qualification_requirements(
            qualification_questions(info.ai_playbook)
        )["requirements"]
        return _config(organization=self.organization, requirements=requirements), requirements

    def test_numbered_mapping_resolves_to_existing_attribute(self):
        AttributeDefinition.objects.create(
            organization=self.organization, key="budget", name="Budget", field_type="text",
        )
        config, requirements = self._playbook(mapping="1. Q1 -> Budget")
        self.assertEqual(config["mappings"][requirements[0]["id"]], "budget")
        self.assertFalse(config["errors"])

    def test_completion_source_stage_is_not_a_destination(self):
        config, _ = self._playbook(
            routing="When all questions are answered, move the lead from New leads to Qualified."
        )
        self.assertEqual(config["completion_stage"]["id"], self.qualified.id)
        self.assertNotIn(str(self.new_lead.id), config["protected_completion_stage_ids"])

    def test_custom_completion_target_uses_current_pipeline(self):
        target = Stage.objects.create(
            pipeline=self.pipeline, name="Sales Review", display_order=100, is_active=True,
        )
        other_pipeline = Pipeline.objects.create(organization=self.organization, name="Other Sales")
        Stage.objects.create(
            pipeline=other_pipeline, name="Sales Review", display_order=100, is_active=True,
        )
        config, _ = self._playbook(
            routing="When all Qualification Criteria are satisfied:\n"
                    "Move the lead to Sales Review in the current pipeline."
        )
        with patch("apps.ai_engagement.services.playbook.criteria_for_lead", return_value={"qualified": True}):
            chosen = _completion_target(lead=self.lead, state={}, config=config)
        self.assertEqual(chosen["id"], target.id)

    def test_failed_criteria_and_ambiguous_targets_remain_blocked(self):
        config, _ = self._playbook(
            routing="When all questions are answered, move to Qualified."
        )
        with patch("apps.ai_engagement.services.playbook.criteria_for_lead", return_value={"qualified": False}):
            self.assertIsNone(_completion_target(lead=self.lead, state={}, config=config))
        other_pipeline = Pipeline.objects.create(organization=self.organization, name="Other Sales")
        self.assertTrue(other_pipeline.stages.filter(name="Qualified").exists())
        config, _ = self._playbook(
            routing="When all questions are answered, move to Qualified."
        )
        with patch("apps.ai_engagement.services.playbook.criteria_for_lead", return_value={"qualified": True}):
            self.assertIsNone(_completion_target(lead=self.lead, state={}, config=config))

    def test_unresolved_authored_completion_never_falls_back_to_qualified(self):
        config, _ = self._playbook(
            routing="When all questions are answered, move to Missing Review in the current pipeline."
        )
        with patch("apps.ai_engagement.services.playbook.criteria_for_lead", return_value={"qualified": True}):
            self.assertIsNone(_completion_target(
                lead=self.lead, state={"qualified_stage_id": str(self.qualified.id)}, config=config,
            ))

    def test_instagram_confirmation_uses_its_own_previous_reply(self):
        from apps.ai_engagement.services.stage_transition_evidence import _filter_stage_actions
        from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage

        target = Stage.objects.create(
            pipeline=self.pipeline, name="Call Requested", display_order=100, is_active=True,
        )
        self._playbook(
            routing="When the lead requests a call, move the lead to Call Requested in the current pipeline."
        )
        account = InstagramAccount.objects.create(
            organization=self.organization, ig_user_id="action-regression-ig",
        )
        conversation = InstagramConversation.objects.create(
            organization=self.organization, account=account, lead=self.lead, participant_id="customer-1",
        )
        InstagramMessage.objects.create(
            organization=self.organization, account=account, conversation=conversation,
            direction="outbound", body="Would you like a call?", status="sent",
        )
        source = InstagramMessage.objects.create(
            organization=self.organization, account=account, conversation=conversation,
            direction="inbound", body="Yes please", status="received",
        )
        action = {"type": "pipeline_transition", "stage_shift": {"stage_id": str(target.id)}}
        self.assertEqual(_filter_stage_actions(
            organization=self.organization, lead=self.lead, actions=[action], source_message=source,
        ), [action])
        source.body = "No thanks"
        source.save(update_fields=["body"])
        self.assertEqual(_filter_stage_actions(
            organization=self.organization, lead=self.lead, actions=[action], source_message=source,
        ), [])
