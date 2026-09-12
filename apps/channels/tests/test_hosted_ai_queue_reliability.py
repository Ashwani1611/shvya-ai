from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.hosted_automation.models import HostedAutomationJob, HostedFollowupStepConfig
from apps.organizations.models import Organization
from services.channels.hosted_chat_service import handle_hosted_gateway_event


class HostedAIIdentityRepairTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Identity AI",
            settings={"hosted_account_enabled": True},
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Sales",
            country_code="+91",
            phone_number="8700274739",
            ai_enabled=True,
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sales",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        settings = dict(self.organization.settings or {})
        settings["hosted_whatsapp"] = {
            "sessions": {
                str(self.account.id): {
                    "ai_auto_reply": True,
                    "auto_lead_creation": False,
                    "auto_follow_up": True,
                }
            }
        }
        self.organization.settings = settings
        self.organization.save(update_fields=["settings", "updated_at"])
        self.account.organization = self.organization
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="LID Lead",
            phone="+919876543210",
        )

    @patch("apps.hosted_automation.signals.dispatch_due_hosted_ai.apply_async")
    def test_lid_identity_repair_still_creates_hosted_ai_job(self, apply_async):
        payload = {
            "sessionId": str(self.account.id),
            "event": "message",
            "messageId": "LID-AI-1",
            "from": "109698229481548@lid",
            "to": "918700274739@c.us",
            "fromMe": False,
            "body": "Can you help me?",
            "messageType": "text",
            "timestamp": timezone.now().timestamp(),
            "chatId": "109698229481548@lid",
            "rawChatId": "109698229481548@lid",
            "peerKey": "+919876543210",
            "peerPhone": "",
            "contactPhoneNumber": "",
            "contactName": "LID Lead",
            "isGroup": False,
        }

        with self.captureOnCommitCallbacks(execute=True):
            message = handle_hosted_gateway_event(payload=payload)

        message.refresh_from_db()
        self.assertEqual(message.lead_id, self.lead.id)
        job = HostedAutomationJob.objects.get(source_message=message)
        self.assertEqual(job.account_id, self.account.id)
        self.assertEqual(job.lead_id, self.lead.id)
        self.assertEqual(job.status, HostedAutomationJob.Status.QUEUED)
        apply_async.assert_called()


class HostedQueueSourceOfTruthTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Queue Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-queue@example.com",
            password="test-password",
            name="Hosted Queue Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Queue Sales",
            country_code="+91",
            phone_number="8700274739",
            ai_enabled=True,
            owner=self.user,
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Queue Sales",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        settings = dict(self.organization.settings or {})
        settings["hosted_whatsapp"] = {
            "sessions": {
                str(self.account.id): {
                    "ai_auto_reply": True,
                    "auto_follow_up": True,
                }
            }
        }
        self.organization.settings = settings
        self.organization.save(update_fields=["settings", "updated_at"])
        self.account.organization = self.organization
        AutoFollowupSettings.objects.create(
            organization=self.organization,
            enabled=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Queue Lead",
            phone="+919876543210",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def test_queue_uses_real_jobs_and_next_sequence_execution_time(self):
        now = timezone.now()
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="queue-inbound",
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="Tell me more",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        job = HostedAutomationJob.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            source_message=inbound,
            available_at=now + timedelta(minutes=2),
        )

        sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="Queue Sequence",
            whatsapp_account=self.account,
            created_by=self.user,
            is_active=True,
        )
        step = FollowupStep.objects.create(
            sequence=sequence,
            position=1,
            step_type=FollowupStep.StepType.WHATSAPP,
            title="First follow-up",
            schedule_type=FollowupStep.ScheduleType.DELAY,
            delay_value=1,
            delay_unit=FollowupStep.DelayUnit.MINUTES,
        )
        HostedFollowupStepConfig.objects.create(
            step=step,
            body="Following up on your enquiry",
            authored_content_hash="a" * 64,
        )
        next_send = now + timedelta(minutes=1)
        state = LeadSequenceState.objects.create(
            organization=self.organization,
            lead=self.lead,
            sequence=sequence,
            status=LeadSequenceState.Status.ACTIVE,
            lead_auto_followup_enabled=True,
            next_step=step,
            upcoming_send_at=next_send,
        )

        # This row is already represented by the durable AI job and must not
        # appear as a duplicate card in the queue.
        WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number=self.lead.phone,
            body="Generated AI reply",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_ai": {
                    "source_inbound_message_id": str(inbound.id),
                }
            },
        )

        response = self.client.get(
            reverse("whatsapp-hosted-session-queue", args=[self.account.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["id"], str(state.id))
        self.assertEqual(payload["items"][0]["next_step"], "First follow-up")
        self.assertEqual(payload["items"][0]["available_at"], next_send.isoformat())
        self.assertEqual(payload["next_execution_at"], next_send.isoformat())
        self.assertEqual(payload["items"][1]["id"], str(job.id))
        self.assertIn("AI reply to: Tell me more", payload["items"][1]["body"])
