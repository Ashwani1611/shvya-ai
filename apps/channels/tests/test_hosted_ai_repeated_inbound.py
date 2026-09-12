from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import (
    create_hosted_account,
    handle_gateway_event,
    update_session_settings,
)


class HostedAIRepeatedInboundTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Always Engage Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-always-engage@example.com",
            password="test-password",
            name="Hosted Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Hosted Sales",
            country_code="+91",
            phone_number="8700274739",
            owner=self.user,
            ai_enabled=True,
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.account, _pipeline, _created = create_hosted_account(
            organization=self.organization,
            created_by=self.user,
            country_code="+91",
            phone_number="8700274739",
        )
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status", "updated_at"])
        update_session_settings(
            account=self.account,
            payload={"ai_auto_reply": True},
        )

        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Repeat Lead",
            phone="+919876543210",
            ai_enabled=False,
        )
        self.stage.ai_on = False
        self.stage.save(update_fields=["ai_on", "updated_at"])

    def _receive(self, *, message_id, body):
        # Creating the durable job also schedules its delayed dispatcher after
        # commit. Keep this test scoped to inbound->job creation rather than
        # executing the Celery task eagerly (which would call the AI provider).
        with patch(
            "apps.hosted_automation.signals.dispatch_due_hosted_ai.apply_async"
        ):
            with self.captureOnCommitCallbacks(execute=True):
                return handle_gateway_event(
                    payload={
                        "sessionId": str(self.account.id),
                        "event": "message",
                        "messageId": message_id,
                        "from": "919876543210@c.us",
                        "to": "918700274739@c.us",
                        "chatId": "919876543210@c.us",
                        "contactName": "Repeat Lead",
                        "body": body,
                        "messageType": "text",
                        "isGroup": False,
                    }
                )

    def test_every_new_hosted_inbound_creates_an_ai_job(self):
        first = self._receive(message_id="HOSTED-TURN-1", body="Hello")
        second = self._receive(
            message_id="HOSTED-TURN-2",
            body="I have another question",
        )

        jobs = list(
            HostedAutomationJob.objects.filter(
                organization=self.organization,
                account=self.account,
                lead=self.lead,
            ).order_by("created_at", "id")
        )

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].source_message_id, first.id)
        self.assertEqual(jobs[1].source_message_id, second.id)
        self.assertEqual(jobs[0].status, HostedAutomationJob.Status.QUEUED)
        self.assertEqual(jobs[1].status, HostedAutomationJob.Status.QUEUED)
