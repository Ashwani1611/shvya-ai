from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.analytics.analytics_service import get_ai_welcome_trend


class InsightsAnalyticsServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Insights Org")
        self.user = User.objects.create_user(
            email="insights-admin@example.com",
            password="test-password",
            name="Insights Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Insights Sales",
            country_code="+91",
            phone_number="9876543210",
            owner=self.user,
        )
        self.stage = Stage.objects.get(pipeline=self.pipeline, name="New leads")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Insights Lead",
            phone="+919000000001",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Insights Sender",
            phone_number_id="123456789",
            display_phone_number="+919876543210",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _outbound(self, *, body, raw_payload):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number=self.lead.phone,
            body=body,
            status=WhatsAppMessage.Status.SENT,
            raw_payload=raw_payload,
        )

    def test_ai_trend_counts_historical_messages_without_explicit_engagement_origin(self):
        self._outbound(
            body="Historical AI reply",
            raw_payload={"shvya_ai": {"source_inbound_message_id": "source-1"}},
        )
        self._outbound(
            body="Current AI reply",
            raw_payload={"shvya_ai": {"origin": "engagement", "source_inbound_message_id": "source-2"}},
        )
        self._outbound(
            body="Bump up",
            raw_payload={"shvya_ai": {"origin": "bump_up", "number": 1}},
        )
        self._outbound(
            body="Welcome",
            raw_payload={"shvya_welcome": {"trigger": "lead_created"}},
        )

        today = timezone.localdate().isoformat()
        rows = get_ai_welcome_trend(
            organization=self.organization,
            date_from=today,
            date_to=today,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["total_ai"], 3)
        self.assertEqual(row["ai_bumpups"], 1)
        self.assertEqual(row["ai_replies"], 2)
        self.assertEqual(row["welcome_messages"], 1)
