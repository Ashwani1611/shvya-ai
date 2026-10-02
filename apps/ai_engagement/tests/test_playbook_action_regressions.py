"""Authored mappings, completion routing and channel evidence regressions."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import qualification_questions
from apps.ai_engagement.services.qualification_execution.common import _split_mapping
from apps.ai_engagement.services.qualification_execution.config import _config
from apps.ai_engagement.services.qualification_execution.completion import _completion_target, _configured_completion_reminders
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

    def test_completion_reminder_obeys_shared_time_validation(self):
        now = datetime(2026, 10, 2, 10, tzinfo=datetime_timezone.utc)
        with patch("apps.ai_engagement.services.reminder_time_runtime.timezone.now", return_value=now):
            for time, delta in (("2 days", timedelta(days=2)), ("1 week", timedelta(weeks=1))):
                actions = _configured_completion_reminders({
                    "reminder_rules": ["After qualification completed create a reminder in " + time],
                })
                self.assertEqual(len(actions), 1)
                self.assertEqual(datetime.fromisoformat(actions[0]["due_at"]), now + delta)
            self.assertEqual(_configured_completion_reminders({
                "reminder_rules": ["After qualification completed create a reminder in 0 days"],
            }), [])

    def test_completion_reminder_does_not_override_prohibitions_or_extra_conditions(self):
        for rule in (
            "After qualification completed, do not create a reminder in 2 days.",
            "After qualification completed, never create a reminder in 2 days.",
            "After qualification completed, if the lead requests a call create a reminder in 2 days.",
            "After qualification completed, create a reminder in 2 days unless the lead declined.",
        ):
            with self.subTest(rule=rule):
                self.assertEqual(_configured_completion_reminders({"reminder_rules": [rule]}), [])


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
        for mapping in ("1. Q1 -> Budget", "Question 1 -> Budget", "Qualification Question 1 -> Budget"):
            with self.subTest(mapping=mapping):
                config, requirements = self._playbook(mapping=mapping)
                self.assertEqual(config["mappings"][requirements[0]["id"]], "budget")
                self.assertFalse(config["errors"])

    def test_completion_source_stage_is_not_a_destination(self):
        config, requirements = self._playbook(
            routing="When all questions are answered, move the lead from New leads to Qualified."
        )
        state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
        chosen = _completion_target(lead=self.lead, state=state, config=config)
        self.assertEqual(chosen["id"], self.qualified.id)
        self.assertEqual(chosen["pipeline_id"], self.pipeline.id)
        self.assertNotIn(str(self.new_lead.id), config["protected_completion_stage_ids"])

    def test_implicit_completion_target_stays_in_current_pipeline(self):
        other_pipeline = Pipeline.objects.create(organization=self.organization, name="Other Sales")
        Stage.objects.create(pipeline=self.pipeline, name="Qualification", display_order=90)
        for routing in (
            "When all questions are answered, move to Qualified.",
            "Transition the lead to Qualified when qualification is completed.",
        ):
            with self.subTest(routing=routing):
                config, requirements = self._playbook(routing=routing)
                state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
                self.assertEqual(_completion_target(lead=self.lead, state=state, config=config)["id"], self.qualified.id)
                self.assertNotEqual(self.qualified.pipeline_id, other_pipeline.id)

    def test_explicit_pipeline_and_overlapping_stage_name(self):
        other_pipeline = Pipeline.objects.create(organization=self.organization, name="Other Sales")
        Stage.objects.create(pipeline=other_pipeline, name="Review", display_order=90)
        target = Stage.objects.create(pipeline=other_pipeline, name="Sales Review", display_order=100)
        config, requirements = self._playbook(
            routing="When all questions are answered, move to Sales Review in the Other Sales pipeline."
        )
        state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
        self.assertEqual(_completion_target(lead=self.lead, state=state, config=config)["id"], target.id)

    def test_completion_does_not_infer_foreign_or_unknown_pipeline(self):
        other_pipeline = Pipeline.objects.create(organization=self.organization, name="Other Sales")
        Stage.objects.create(pipeline=other_pipeline, name="Sales Review", display_order=100)
        for routing in (
            "When all questions are answered, move to Sales Review.",
            "When all questions are answered, move to Qualified in Missing Sales pipeline.",
        ):
            with self.subTest(routing=routing):
                config, requirements = self._playbook(routing=routing)
                state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
                self.assertIsNone(_completion_target(lead=self.lead, state=state, config=config))

    def test_completion_routes_use_the_lead_acquisition_source(self):
        instagram_stage = Stage.objects.create(pipeline=self.pipeline, name="Instagram Review", display_order=90)
        whatsapp_stage = Stage.objects.create(pipeline=self.pipeline, name="WhatsApp Review", display_order=91)
        config, requirements = self._playbook(routing=(
            "Rule 1:\nWhen lead source is Instagram and all questions are answered, move to Instagram Review.\n"
            "Rule 2:\nWhen lead source is WhatsApp API and all questions are answered, move to WhatsApp Review."
        ))
        state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
        for source, expected in (("instagram", instagram_stage), ("whatsapp_api", whatsapp_stage), ("system", None)):
            with self.subTest(source=source):
                self.lead.lead_source = source
                target = _completion_target(lead=self.lead, state=state, config=config)
                self.assertEqual(target["id"] if target else None, expected.id if expected else None)

    def test_instagram_source_completion_runs_on_a_whatsapp_turn(self):
        from apps.ai_engagement.services.qualification_execution_contract import resolve_before_generation
        from apps.ai_engagement.services.qualification_state import record_last_asked_requirement

        target = Stage.objects.create(pipeline=self.pipeline, name="Instagram Review", display_order=90)
        _, requirements = self._playbook(routing=(
            "For Instagram leads, when all questions are answered, move to Instagram Review."
        ))
        self.lead.lead_source = "instagram"
        self.lead.save(update_fields=["lead_source"])
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        source = AIEngagementControlTests._inbound(self, "instagram-acquired-whatsapp-turn")
        source.body = "50000"
        source.save(update_fields=["body"])
        result = resolve_before_generation(
            organization=self.organization, lead=self.lead, source_message_id=source.id,
        )
        self.assertTrue(result.get("applied"), result)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, target.id)

    def test_unsupported_source_conditions_do_not_become_unconditional(self):
        for condition in ("source is Unknown Network", "source is not Instagram", "source is Instagram or WhatsApp"):
            with self.subTest(condition=condition):
                config, requirements = self._playbook(routing=(
                    f"When lead {condition} and all questions are answered, move to Qualified."
                ))
                state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": "50000"}}}
                self.lead.lead_source = "instagram"
                self.assertIsNone(_completion_target(lead=self.lead, state=state, config=config))

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
        config, _ = self._playbook(
            routing="When all questions are answered, move to Qualified or Nurturing."
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
