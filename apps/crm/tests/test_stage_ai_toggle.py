from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.crm.models import Lead, Stage
from apps.organizations.models import Organization


class StageAIToggleTests(TestCase):

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Stage AI Test Organization"
        )

        self.user = User.objects.create_user(
            email="stage-ai-test@example.com",
            organization=self.organization,
            password="test-password",
            role=User.Role.ADMIN,
            name="Stage AI Test User",
        )

        self.pipeline = self.organization.pipelines.get(
            is_active=True
        )

        self.stage = (
            Stage.objects
            .filter(
                pipeline=self.pipeline,
                is_active=True,
            )
            .order_by("display_order", "name")
            .first()
        )

        self.assertIsNotNone(self.stage)

        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="AI Toggle Lead",
            phone="+919876543210",
        )

        self._authenticate_crm_user()

    def _authenticate_crm_user(self):
        crm_session = SessionStore()

        set_authenticated_user(
            crm_session,
            self.user,
        )

        crm_session.save()

        cookie_name = get_session_cookie_name("dashboard")

        self.client.cookies[cookie_name] = crm_session.session_key

    def test_toggle_stage_ai_off(self):
        self.stage.ai_on = True
        self.stage.save(update_fields=["ai_on"])

        response = self.client.post(
            reverse(
                "crm-stage-ai-toggle",
                kwargs={"stage_id": self.stage.id},
            )
        )

        self.assertEqual(response.status_code, 200)

        self.stage.refresh_from_db()

        self.assertFalse(self.stage.ai_on)

        self.assertContains(response, "AI Off")

    def test_toggle_stage_ai_on(self):
        self.stage.ai_on = False
        self.stage.save(update_fields=["ai_on"])

        response = self.client.post(
            reverse(
                "crm-stage-ai-toggle",
                kwargs={"stage_id": self.stage.id},
            )
        )

        self.assertEqual(response.status_code, 200)

        self.stage.refresh_from_db()

        self.assertTrue(self.stage.ai_on)

        self.assertContains(response, "AI On")

    def test_toggle_lead_ai_off_and_on(self):
        response = self.client.post(
            reverse("crm-lead-ai-toggle", kwargs={"lead_id": self.lead.id})
        )
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.ai_enabled)
        self.assertContains(response, "AI Off")

        response = self.client.post(
            reverse("crm-lead-ai-toggle", kwargs={"lead_id": self.lead.id})
        )
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.ai_enabled)
        self.assertContains(response, "AI On")

    def test_user_cannot_toggle_lead_from_another_organization(self):
        other_organization = Organization.objects.create(name="Other Lead AI Organization")
        other_pipeline = other_organization.pipelines.get(is_active=True)
        other_stage = other_pipeline.stages.filter(is_active=True).first()
        other_lead = Lead.objects.create(
            organization=other_organization,
            pipeline=other_pipeline,
            stage=other_stage,
            name="Other Lead",
            phone="+919876543211",
        )

        response = self.client.post(
            reverse("crm-lead-ai-toggle", kwargs={"lead_id": other_lead.id})
        )
        self.assertEqual(response.status_code, 404)
        other_lead.refresh_from_db()
        self.assertTrue(other_lead.ai_enabled)

    def test_user_cannot_toggle_stage_from_another_organization(self):
        other_organization = Organization.objects.create(
            name="Other Stage AI Organization"
        )

        other_pipeline = other_organization.pipelines.get(
            is_active=True
        )

        other_stage = (
            Stage.objects
            .filter(
                pipeline=other_pipeline,
                is_active=True,
            )
            .order_by("display_order", "name")
            .first()
        )

        self.assertIsNotNone(other_stage)

        response = self.client.post(
            reverse(
                "crm-stage-ai-toggle",
                kwargs={"stage_id": other_stage.id},
            )
        )

        self.assertEqual(response.status_code, 404)

        other_stage.refresh_from_db()

        self.assertTrue(other_stage.ai_on)
