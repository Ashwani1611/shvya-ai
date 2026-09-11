from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.playground import PlaygroundService
from apps.ai_engagement.services.qualification_state import (
    REQUIREMENT_ANSWERED,
    REQUIREMENT_NOT_APPLICABLE,
    STATUS_COMPLETED,
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class ConditionalQualificationRuntimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Conditional Qualification Org")
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Conditional Sales",
            is_active=True,
        )
        cls.new_stage = (
            cls.pipeline.stages.filter(name__in=["New Lead", "New leads"])
            .order_by("display_order", "name")
            .first()
        )
        if cls.new_stage is None:
            used = set(cls.pipeline.stages.values_list("display_order", flat=True))
            order = 0
            while order in used:
                order += 1
            cls.new_stage = Stage.objects.create(
                pipeline=cls.pipeline,
                name="New Lead",
                display_order=order,
                is_active=True,
            )

    def setUp(self):
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_stage,
            name="Conditional Test Lead",
            phone="+919900001234",
            attributes={},
            ai_enabled=True,
        )

    def _requirements(self):
        return compile_qualification_requirements(
            "Do you currently run Meta ads?\n"
            "A. Yes\n"
            "B. No\n"
            "Where do most of your leads come from if you're not using ads? optional"
        )["requirements"]

    def test_compiler_creates_machine_evaluable_conditional_requirement(self):
        requirements = self._requirements()
        ads, lead_source = requirements

        self.assertFalse(lead_source["required"])
        self.assertTrue(lead_source["conditional"])
        self.assertEqual(
            lead_source["eligible_when"],
            {
                "requirement_id": ads["id"],
                "operator": "eq",
                "value": False,
            },
        )
        self.assertNotIn("if you're not using ads", lead_source["question"].casefold())

    def test_running_ads_yes_marks_lead_source_not_applicable_and_completes(self):
        ads, lead_source = self._requirements()
        requirements = [ads, lead_source]
        record_last_asked_requirement(self.lead, ads["id"], requirements=requirements)

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="A",
            source_message_id="ads-yes",
        )

        state = result["state"]
        self.assertEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(
            state["requirement_states"][ads["id"]]["status"],
            REQUIREMENT_ANSWERED,
        )
        self.assertEqual(
            state["requirement_states"][lead_source["id"]]["status"],
            REQUIREMENT_NOT_APPLICABLE,
        )
        self.assertIsNone(state["current_requirement_id"])
        self.assertEqual(state["conversation_mode"], "qualified")

    def test_running_ads_no_makes_lead_source_the_next_requirement(self):
        ads, lead_source = self._requirements()
        requirements = [ads, lead_source]
        record_last_asked_requirement(self.lead, ads["id"], requirements=requirements)

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="B",
            source_message_id="ads-no",
        )

        state = result["state"]
        self.assertNotEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(state["current_requirement_id"], lead_source["id"])
        self.assertEqual(state["conversation_mode"], "qualifying")

    def test_referral_closes_eligible_lead_source_requirement(self):
        ads, lead_source = self._requirements()
        requirements = [ads, lead_source]
        record_last_asked_requirement(self.lead, ads["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="B",
            source_message_id="ads-no",
        )
        record_last_asked_requirement(
            self.lead,
            lead_source["id"],
            requirements=requirements,
        )

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="Referral",
            source_message_id="lead-source",
        )

        self.assertEqual(
            result["state"]["requirement_states"][lead_source["id"]]["status"],
            REQUIREMENT_ANSWERED,
        )
        self.assertEqual(
            result["state"]["requirement_states"][lead_source["id"]]["value"],
            "Referral",
        )
        self.assertEqual(result["state"]["qualification_status"], STATUS_COMPLETED)

    def test_already_mentioned_is_not_stored_as_an_answer(self):
        requirements = compile_qualification_requirements(
            "Where do you manage leads?"
        )["requirements"]
        requirement = requirements[0]
        record_last_asked_requirement(
            self.lead,
            requirement["id"],
            requirements=requirements,
        )

        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="I already mentioned",
            source_message_id="recovery-signal",
        )

        self.assertFalse(result["changed"])
        self.assertTrue(result.get("recovery_requested"))
        state = state_for_lead(self.lead, requirements=requirements)
        self.assertNotEqual(
            state["requirement_states"][requirement["id"]]["status"],
            REQUIREMENT_ANSWERED,
        )


class PlaygroundRestartTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = SimpleNamespace(id="org-playground-reset")
        self.service = PlaygroundService()

    def test_reset_deletes_entire_backend_playground_session(self):
        self.service._save_history(
            organization=self.organization,
            session_id="old-session",
            history=[
                {"role": "user", "content": "Old question"},
                {"role": "assistant", "content": "Old answer"},
            ],
        )
        self.assertIsNotNone(
            self.service._load_history(
                organization=self.organization,
                session_id="old-session",
            )
        )

        self.service.reset(
            organization=self.organization,
            session_id="old-session",
        )

        self.assertIsNone(
            self.service._load_history(
                organization=self.organization,
                session_id="old-session",
            )
        )

    def test_playground_restart_client_calls_backend_delete_before_new_session(self):
        script = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "ai_setup_playground.js"
        ).read_text(encoding="utf-8")

        self.assertIn('method: "DELETE"', script)
        self.assertIn("session_id: oldSessionId", script)
        delete_index = script.index('method: "DELETE"')
        new_session_index = script.index(
            "page.dataset.playgroundSessionId = createSessionId()",
            delete_index,
        )
        self.assertLess(delete_index, new_session_index)
