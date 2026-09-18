from django.test import TestCase

from apps.ai_engagement.services.intent_score import (
    INTENT_SCORE_STATE_KEY,
    compute_intent_score,
    persist_intent_score,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class IntentScoreTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Intent Score Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            is_active=True,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            is_active=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            phone_number_id="intent-score-account",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Intent Lead",
            phone="+919999000111",
            attributes={},
        )

    def _inbound(self, body):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.RECEIVED,
            body=body,
            from_number=self.lead.phone,
            to_number="+919000000000",
        )

    def test_score_uses_exact_0_to_10_rubric_without_provider_call(self):
        self._inbound("We need to automate lead follow-ups in our CRM.")
        self._inbound("We need this within 7 days.")
        self._inbound("Please schedule a demo. Our budget is 28000.")
        self._inbound("We handle around 180 customer conversations every month.")

        result = compute_intent_score(lead=self.lead)

        self.assertEqual(result["score"], 10)
        self.assertEqual(result["components"]["engagement"]["score"], 3)
        self.assertEqual(result["components"]["urgency"]["score"], 3)
        self.assertEqual(result["components"]["clarity"]["score"], 2)
        self.assertEqual(result["components"]["commitment"]["score"], 2)

    def test_score_persists_as_internal_lead_intelligence(self):
        self._inbound("I am interested in pricing.")
        result = persist_intent_score(lead=self.lead)
        self.lead.refresh_from_db()

        self.assertIn(INTENT_SCORE_STATE_KEY, self.lead.attributes)
        self.assertEqual(
            self.lead.attributes[INTENT_SCORE_STATE_KEY]["score"],
            result["score"],
        )
        self.assertNotIn("intent_score", {
            key: value
            for key, value in self.lead.attributes.items()
            if key != INTENT_SCORE_STATE_KEY
        })
