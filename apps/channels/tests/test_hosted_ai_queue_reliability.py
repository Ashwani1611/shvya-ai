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
            package="dfy",
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
    def test_live_lid_identity_repair_auto_creates_new_lead_and_ai_job(self, apply_async):
        settings = dict(self.organization.settings or {})
        settings["hosted_whatsapp"]["sessions"][str(self.account.id)]["auto_lead_creation"] = True
        self.organization.settings = settings
        self.organization.save(update_fields=["settings", "updated_at"])

        phone = "+919811223344"
        payload = {
            "sessionId": str(self.account.id),
            "event": "message",
            "messageId": "LID-NEW-LEAD-AI-1",
            "from": "109698229481999@lid",
            "to": "918700274739@c.us",
            "fromMe": False,
            "body": "Hello, I need details",
            "messageType": "text",
            "timestamp": timezone.now().timestamp(),
            "chatId": "109698229481999@lid",
            "rawChatId": "109698229481999@lid",
            "peerKey": phone,
            "peerPhone": "",
            "contactPhoneNumber": "",
            "contactName": "New LID Prospect",
            "isGroup": False,
        }

        with self.captureOnCommitCallbacks(execute=True):
            message = handle_hosted_gateway_event(payload=payload)

        message.refresh_from_db()
        lead = Lead.objects.get(organization=self.organization, phone=phone)
        self.assertEqual(message.lead_id, lead.id)
        self.assertEqual(lead.pipeline_id, self.pipeline.id)
        self.assertEqual(lead.stage_id, self.stage.id)
        self.assertEqual(lead.lead_source, "whatsapp")
        job = HostedAutomationJob.objects.get(source_message=message)
        self.assertEqual(job.account_id, self.account.id)
        self.assertEqual(job.lead_id, lead.id)
        self.assertEqual(job.status, HostedAutomationJob.Status.QUEUED)
        apply_async.assert_called()


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
            package="dfy",
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

    def _ai_job(self, *, status=HostedAutomationJob.Status.QUEUED, started_at=None):
        inbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=f"recovery-{HostedAutomationJob.objects.count()}",
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="Please send details",
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={},
        )
        job, _ = HostedAutomationJob.objects.update_or_create(
            source_message=inbound,
            defaults={
                "organization": self.organization,
                "account": self.account,
                "lead": self.lead,
                "status": status,
                "started_at": started_at,
                "available_at": timezone.now() - timedelta(seconds=1),
            },
        )
        return job

    def test_dispatcher_keeps_job_queued_until_processing_task_claims_it(self):
        from services.channels.hosted_automation_service import (
            dispatch_one_hosted_ai_job,
        )

        job = self._ai_job()
        with (
            patch(
                "services.channels.hosted_automation_service.hosted_ai_block_reason",
                return_value="",
            ),
            patch(
                "services.channels.hosted_automation_service.automation_pause_until",
                return_value=None,
            ),
            patch(
                "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task.delay"
            ) as publish,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = dispatch_one_hosted_ai_job()

        job.refresh_from_db()
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(job.status, HostedAutomationJob.Status.QUEUED)
        self.assertIsNone(job.started_at)
        self.assertGreater(job.lease_expires_at, timezone.now())
        self.assertLess(job.available_at, timezone.now())
        publish.assert_called_once_with(str(job.id))

    def test_stale_processing_job_returns_to_queue_and_is_dispatched(self):
        from services.channels.hosted_automation_service import (
            HOSTED_AI_PROCESSING_STALE_SECONDS,
            dispatch_one_hosted_ai_job,
        )

        job = self._ai_job(
            status=HostedAutomationJob.Status.PROCESSING,
            started_at=timezone.now()
            - timedelta(seconds=HOSTED_AI_PROCESSING_STALE_SECONDS + 5),
        )
        with (
            patch(
                "services.channels.hosted_automation_service.hosted_ai_block_reason",
                return_value="",
            ),
            patch(
                "services.channels.hosted_automation_service.automation_pause_until",
                return_value=None,
            ),
            patch(
                "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task.delay"
            ) as publish,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = dispatch_one_hosted_ai_job()

        job.refresh_from_db()
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(job.status, HostedAutomationJob.Status.QUEUED)
        self.assertIsNone(job.started_at)
        self.assertEqual((job.result or {}).get("recovery_reason"), "stale_processing_lease")
        self.assertEqual((job.result or {}).get("recovery_count"), 1)
        publish.assert_called_once_with(str(job.id))

    def test_processing_task_resumes_orphaned_generated_message_without_regeneration(self):
        from apps.hosted_automation.tasks import process_hosted_ai_engagement_job_task

        job = self._ai_job()
        outbound = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.account.display_phone_number,
            to_number=self.lead.phone,
            body="Recovered AI reply",
            status=WhatsAppMessage.Status.QUEUED,
            raw_payload={
                "shvya_ai": {
                    "source_inbound_message_id": str(job.source_message_id),
                    "provider": "hosted",
                }
            },
        )

        with (
            patch(
                "apps.hosted_automation.tasks.hosted_ai_block_reason",
                return_value="",
            ),
            patch(
                "apps.hosted_automation.tasks.hosted_health_pause_until",
                return_value=None,
            ),
            patch(
                "apps.hosted_automation.execution.execute_hosted_ai_engagement"
            ) as generate,
            patch(
                "services.channels.hosted_whatsapp_transport.send_hosted_message"
            ) as send,
        ):
            result = process_hosted_ai_engagement_job_task.run(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, HostedAutomationJob.Status.COMPLETED)
        self.assertEqual((job.result or {}).get("message_id"), str(outbound.id))
        self.assertTrue((job.result or {}).get("recovered_generated_message"))
        self.assertEqual((result.get("delivery") or {}).get("status"), "sent")
        generate.assert_not_called()
        send.assert_called_once()

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
        job, _ = HostedAutomationJob.objects.update_or_create(
            source_message=inbound,
            defaults={
                "organization": self.organization,
                "account": self.account,
                "lead": self.lead,
                "available_at": now + timedelta(minutes=2),
            },
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
        self.assertEqual(payload["items"][1]["id"], str(state.id))
        self.assertEqual(payload["items"][1]["next_step"], "First follow-up")
        self.assertEqual(payload["items"][1]["available_at"], next_send.isoformat())
        self.assertEqual(payload["next_execution_at"], next_send.isoformat())
        self.assertEqual(payload["items"][0]["id"], str(job.id))
        self.assertIn("AI reply to: Tell me more", payload["items"][0]["body"])

    def _queue(self):
        response = self.client.get(reverse("whatsapp-hosted-session-queue", args=[self.account.id]))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_queue_welcome_without_inbound_is_first_even_during_backoff(self):
        reply = self._ai_job()
        welcome = HostedAutomationJob.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            kind=HostedAutomationJob.Kind.WELCOME,
            available_at=timezone.now() + timedelta(seconds=30),
        )
        payload = self._queue()
        self.assertEqual([row["id"] for row in payload["items"]], [str(welcome.pk), str(reply.pk)])
        self.assertEqual(payload["items"][0]["message_type"], "Welcome message")
        self.assertEqual(payload["pending_ai_count"], 2)
        self.assertEqual(payload["ai_min_send_gap_seconds"], 45)

    def test_queue_stale_processing_lease_is_reported_as_recovering(self):
        job = self._ai_job(status=HostedAutomationJob.Status.PROCESSING, started_at=timezone.now())
        HostedAutomationJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        row, = self._queue()["items"]
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["effective_status"], "recovering")
        self.assertIn("Waiting for worker recovery", row["origin"])
        self.assertNotIn("Processing now", row["origin"])

    def test_queued_publication_does_not_claim_processing_in_ui(self):
        job = self._ai_job()
        HostedAutomationJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()+timedelta(seconds=30))
        row, = self._queue()["items"]
        self.assertEqual(row["effective_status"], "queued")
        self.assertIn("waiting for worker", row["origin"])

    def test_queue_uses_durable_sender_gap_and_reports_disabled_stage(self):
        from apps.channels.models import AIMessageSendState
        self._ai_job()
        next_send = timezone.now() + timedelta(seconds=45)
        AIMessageSendState.objects.create(account=self.account, next_send_at=next_send)
        row, = self._queue()["items"]
        self.assertEqual(row["available_at"], next_send.isoformat())
        self.assertIn("45-second minimum gap", row["origin"])
        self.stage.ai_on = False
        self.stage.save(update_fields=["ai_on"])
        row, = self._queue()["items"]
        self.assertEqual(row["effective_status"], "blocked")
        self.assertEqual(row["available_at"], "")
        self.assertIn("stage", row["block_reason"])

    def test_queue_shows_pending_bumpup_without_job_and_deduplicates_owned_welcome(self):
        welcome = HostedAutomationJob.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            kind=HostedAutomationJob.Kind.WELCOME, available_at=timezone.now(),
        )
        for metadata, body in (
            ({"shvya_welcome": {"trigger": "lead_created"}}, "Prepared welcome"),
            ({"shvya_ai": {"origin": "bump_up", "number": 1}}, "Pending bump-up"),
        ):
            WhatsAppMessage.objects.create(
                organization=self.organization, account=self.account, lead=self.lead,
                direction="outbound", from_number=self.account.display_phone_number,
                to_number=self.lead.phone, body=body, status="queued", raw_payload=metadata,
            )
        payload = self._queue()
        self.assertEqual(payload["pending_ai_count"], 2)
        self.assertEqual(payload["shown_ai_count"], 2)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["id"], str(welcome.pk))
        self.assertEqual(payload["items"][1]["message_type"], "Bump-up message")
        self.assertEqual(payload["items"][1]["body"], "Pending bump-up")
