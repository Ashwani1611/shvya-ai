from django.test import TestCase

from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.services.qualification_state import (
    MODE_CONVERSATION,
    MODE_QUALIFICATION,
    RESULT_QUALIFIED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    state_for_lead,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class AIEngagementControlTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Acme")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.new_lead = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_lead,
            name="Test Lead",
            phone="+919111111111",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Sales",
            phone_number_id="meta-phone-id",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _inbound(self, external_id="wamid-inbound"):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="Hello",
            status=WhatsAppMessage.Status.RECEIVED,
            is_read=False,
        )

    def test_new_lead_starts_in_application_controlled_qualification_mode(self):
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead)
        self.assertEqual(state["qualification_status"], STATUS_NOT_STARTED)
        self.assertEqual(state["engagement_mode"], MODE_QUALIFICATION)
        self.assertEqual(state["qualified_stage_id"], str(self.qualified.id))

    def test_ai_qualification_reply_marks_state_in_progress(self):
        self._inbound()
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number=self.lead.phone,
            body="What budget range are you considering?",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={"shvya_ai": {"source_inbound_message_id": "source"}},
        )
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead)
        self.assertEqual(state["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(state["engagement_mode"], MODE_QUALIFICATION)

    def test_moving_to_qualified_completes_and_never_restarts(self):
        self._inbound()
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead)
        self.assertEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(state["qualification_result"], RESULT_QUALIFIED)
        self.assertEqual(state["engagement_mode"], MODE_CONVERSATION)
        self.assertIsNotNone(state["qualification_completed_at"])

        self.lead.stage = self.new_lead
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        state = state_for_lead(self.lead)
        self.assertEqual(state["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(state["qualification_result"], RESULT_QUALIFIED)
        self.assertEqual(state["engagement_mode"], MODE_CONVERSATION)

    def test_ai_permission_requires_pipeline_linked_whatsapp_number(self):
        self._inbound()
        decision = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertTrue(decision.allowed)

        self.account.display_phone_number = "+918888888888"
        self.account.save(update_fields=["display_phone_number", "updated_at"])
        decision = AIPermissionService().evaluate(
            organization=self.organization,
            lead=self.lead,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "pipeline_whatsapp_account_mismatch")

    def test_queued_ai_message_is_cancelled_when_current_stage_ai_is_off(self):
        self._inbound()
        self.new_lead.ai_on = False
        self.new_lead.save(update_fields=["ai_on", "updated_at"])
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number=self.lead.phone,
            body="This must not send",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={"shvya_ai": {"source_inbound_message_id": "source"}},
        )
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("stage_ai_disabled", message.error)

    def test_system_stages_remain_canonically_named_and_active(self):
        self.new_lead.name = "Incoming"
        self.new_lead.is_active = False
        self.new_lead.save(update_fields=["name", "is_active", "updated_at"])
        self.new_lead.refresh_from_db()
        self.assertEqual(self.new_lead.name, "New leads")
        self.assertTrue(self.new_lead.is_active)

        self.qualified.name = "Won"
        self.qualified.is_active = False
        self.qualified.save(update_fields=["name", "is_active", "updated_at"])
        self.qualified.refresh_from_db()
        self.assertEqual(self.qualified.name, "Qualified")
        self.assertTrue(self.qualified.is_active)

    def test_disabled_lead_releases_hosted_sequence_priority(self):
        from apps.hosted_automation.models import HostedAutomationJob
        from services.channels.hosted_automation_service import enqueue_ai_engagement, has_pending_ai
        self.account.connection_type = "hosted"
        self.account.save()
        message = self._inbound()
        # The webhook already created the inbound row; use the persisted source.
        message = self.lead.whatsapp_messages.get(external_id="wamid-inbound")
        job = enqueue_ai_engagement(account=self.account, lead=self.lead, source_message=message)
        self.lead.ai_enabled = False
        self.lead.save(update_fields=["ai_enabled"])
        self.assertFalse(has_pending_ai(account=self.account))
        job.refresh_from_db()
        self.assertEqual(job.status, HostedAutomationJob.Status.SKIPPED)
        self.assertEqual(job.result["reason"], "lead_ai_disabled")

    def test_resumed_hosted_reply_is_cancelled_before_delivery(self):
        from unittest.mock import patch
        from django.utils import timezone
        from apps.hosted_automation.models import HostedAutomationJob
        from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task
        self.account.connection_type = "hosted"
        self.account.save()
        self._inbound()
        inbound = self.lead.whatsapp_messages.get(external_id="wamid-inbound")
        outbound = WhatsAppMessage.objects.create(organization=self.organization, account=self.account, lead=self.lead, direction="outbound", status="queued", body="Paused reply", raw_payload={"shvya_ai": {"source_inbound_message_id": str(inbound.pk)}})
        job = HostedAutomationJob.objects.create(organization=self.organization, account=self.account, lead=self.lead, source_message=inbound, available_at=timezone.now(), result={"message_id": str(outbound.pk)})
        self.new_lead.ai_on = False
        self.new_lead.save(update_fields=["ai_on"])
        with patch("services.channels.hosted_whatsapp_transport.send_hosted_message") as send:
            result = process_hosted_ai_engagement_job_task.run(str(job.pk))
        self.assertEqual(result["reason"], "stage_ai_disabled")
        send.assert_not_called()
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, "failed")

    def test_context_has_qualification_state_without_changing_custom_attributes(self):
        from apps.ai_engagement.services.context import AIContextBuilder
        from apps.ai_engagement.services.qualification_state import QUALIFICATION_STATE_KEY
        context = AIContextBuilder()._build_lead_context(lead=self.lead)
        self.assertEqual(context["qualification"]["engagement_mode"], MODE_QUALIFICATION)
        self.lead.refresh_from_db()
        self.assertNotIn(QUALIFICATION_STATE_KEY, self.lead.attributes)
