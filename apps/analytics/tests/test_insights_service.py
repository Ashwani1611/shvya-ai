from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.analytics.analytics_service import (
    get_ai_welcome_trend, get_automation_flow_trend, get_overview_metrics,
    get_leads_over_time, get_leads_by_pipeline, get_leads_by_stage,
    get_failed_template_messages_queryset,
)


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

    def test_local_midnight_is_shared_by_cards_and_lead_chart(self):
        # 19:00 UTC on Jan 1 is Jan 2 in the organization's timezone.
        created = datetime(2026, 1, 1, 19, tzinfo=dt_timezone.utc)
        Lead.objects.filter(pk=self.lead.pk).update(created_at=created)
        kwargs = dict(organization=self.organization, date_from="2026-01-02", date_to="2026-01-02")
        with timezone.override("UTC"):
            rows = get_leads_over_time(**kwargs)
            self.assertEqual(str(rows[0]["day"]), "2026-01-02")
            self.assertEqual(rows[0]["count"], 1)
            self.assertEqual(get_overview_metrics(**kwargs)["total_leads"], 1)
            self.assertEqual(sum(r["count"] for r in get_leads_by_pipeline(**kwargs)), 1)
            self.assertEqual(sum(r["count"] for r in get_leads_by_stage(**kwargs)), 1)

    def test_end_date_excludes_next_local_midnight(self):
        Lead.objects.filter(pk=self.lead.pk).update(
            created_at=datetime(2026, 1, 2, 18, 30, tzinfo=dt_timezone.utc)
        )
        self.assertEqual(get_leads_over_time(organization=self.organization,
            date_from="2026-01-02", date_to="2026-01-02"), [])

    def test_message_cards_agree_with_chart_and_exclude_unsent(self):
        for status in ("sent", "delivered", "read", "queued", "failed"):
            msg = self._outbound(body=status, raw_payload={"shvya_ai": {"origin": "engagement"}})
            WhatsAppMessage.objects.filter(pk=msg.pk).update(status=status)
        today = timezone.localdate().isoformat()
        kwargs = dict(organization=self.organization, date_from=today, date_to=today)
        self.assertEqual(get_overview_metrics(**kwargs)["ai_messages"], 3)
        self.assertEqual(sum(r["total_ai"] for r in get_ai_welcome_trend(**kwargs)), 3)

    def test_organization_and_pipeline_isolation(self):
        other = Organization.objects.create(name="Other Insights Org")
        self._outbound(body="Private", raw_payload={"shvya_ai": {"origin": "engagement"}})
        today = timezone.localdate().isoformat()
        self.assertEqual(get_ai_welcome_trend(organization=other, date_from=today, date_to=today), [])
        self.assertEqual(get_leads_over_time(organization=other, date_from=today, date_to=today), [])
        from uuid import uuid4
        kwargs = dict(organization=self.organization, pipeline_ids=[uuid4()], date_from=today, date_to=today)
        self.assertEqual(get_ai_welcome_trend(**kwargs), [])
        self.assertEqual(get_overview_metrics(**kwargs)["total_leads"], 0)

    def test_cadence_uses_completion_date_and_excludes_failed_delivery(self):
        from apps.followups.models import FollowupSequence, FollowupStep, LeadSequenceState, FollowupExecution
        seq = FollowupSequence.objects.create(organization=self.organization, name="Welcome flow", whatsapp_account=self.account)
        step = FollowupStep.objects.create(sequence=seq, step_type="whatsapp")
        state = LeadSequenceState.objects.create(organization=self.organization, lead=self.lead, sequence=seq)
        completed = datetime(2026, 1, 1, 19, tzinfo=dt_timezone.utc)
        msg = self._outbound(body="Sequence send", raw_payload={})
        execution = FollowupExecution.objects.create(organization=self.organization, lead=self.lead,
            sequence=seq, step=step, state=state, scheduled_for=completed, finished_at=completed,
            status="sent", whatsapp_message=msg)
        # Later edits must not move a completed send to another day.
        FollowupExecution.objects.filter(pk=execution.pk).update(updated_at=completed + timedelta(days=5))
        kwargs = dict(organization=self.organization, date_from="2026-01-02", date_to="2026-01-02")
        rows = get_automation_flow_trend(**kwargs, step_type="whatsapp")
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(str(rows[0]["day"]), "2026-01-02")
        self.assertEqual(get_overview_metrics(**kwargs)["whatsapp_automation_messages"], 1)
        WhatsAppMessage.objects.filter(pk=msg.pk).update(status="failed")
        self.assertEqual(get_automation_flow_trend(**kwargs, step_type="whatsapp"), [])
        self.assertEqual(get_overview_metrics(**kwargs)["whatsapp_automation_messages"], 0)

    def test_status_callback_keeps_welcome_attribution(self):
        from services.channels.whatsapp_service import handle_status_update
        msg = self._outbound(body="Welcome", raw_payload={"shvya_welcome": {"trigger": "lead_created"}})
        WhatsAppMessage.objects.filter(pk=msg.pk).update(external_id="wamid.insights-welcome")
        with patch("services.channels.realtime.queue_status_publish"):
            handle_status_update(external_id="wamid.insights-welcome", status="delivered", raw_payload={"status": "delivered"})
        msg.refresh_from_db()
        self.assertEqual(msg.raw_payload["shvya_welcome"]["trigger"], "lead_created")
        self.assertEqual(msg.status, "delivered")

    def test_failed_api_templates_match_card(self):
        msg = self._outbound(body="Failed template", raw_payload={})
        WhatsAppMessage.objects.filter(pk=msg.pk).update(status="failed", media_payload={"transport": "template"})
        today = timezone.localdate().isoformat()
        kwargs = dict(organization=self.organization, date_from=today, date_to=today)
        self.assertEqual(get_failed_template_messages_queryset(**kwargs).count(), 1)
        self.assertEqual(get_overview_metrics(**kwargs)["failed_template_messages"], 1)
