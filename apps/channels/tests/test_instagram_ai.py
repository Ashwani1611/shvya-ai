from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.tenant_guard import TenantGuard
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
)
from apps.crm.models import Lead
from apps.organizations.models import Organization
from services.channels.instagram_ai import execute_instagram_ai_engagement
from services.channels.instagram_leads import extract_instagram_phone


class InstagramAIEngagementTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Instagram AI Org")
        self.pipeline = (
            self.org.pipelines.filter(name__iexact="Leads").first()
            or self.org.pipelines.first()
        )
        self.pipeline.country_code = "+91"
        self.pipeline.ai_enabled = True
        self.pipeline.save(
            update_fields=["country_code", "ai_enabled", "updated_at"]
        )
        self.stage = (
            self.pipeline.stages.filter(name__iexact="New Lead").first()
            or self.pipeline.stages.filter(name__iexact="New Leads").first()
            or self.pipeline.stages.first()
        )
        self.stage.ai_on = True
        self.stage.save(update_fields=["ai_on", "updated_at"])
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Instagram Customer",
            phone="",
            lead_source="instagram",
            ai_enabled=True,
        )
        self.account = InstagramAccount.objects.create(
            organization=self.org,
            ig_user_id="instagram-ai-business",
            username="shvya_test",
            access_token="test-token",
            status=InstagramAccount.Status.CONNECTED,
        )
        self.conversation = InstagramConversation.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            participant_id="instagram-ai-customer",
            participant_username="buyer",
            participant_name="Buyer",
        )
        self.inbound = InstagramMessage.objects.create(
            organization=self.org,
            account=self.account,
            conversation=self.conversation,
            direction=InstagramMessage.Direction.INBOUND,
            status=InstagramMessage.Status.RECEIVED,
            external_id="instagram-ai-inbound-1",
            sender_id=self.conversation.participant_id,
            recipient_id=self.account.ig_user_id,
            body="Hello, can you tell me more?",
            is_read=False,
            sent_at=timezone.now(),
        )

    def task(self):
        return SimpleNamespace(
            request=SimpleNamespace(retries=0),
            retry=Mock(side_effect=AssertionError("Unexpected Instagram AI retry")),
        )

    def test_phone_capture_accepts_explicit_contact_evidence_not_arbitrary_numbers(self):
        self.assertEqual(
            extract_instagram_phone("98765 43210", country_code="+91"),
            "+919876543210",
        )
        self.assertEqual(
            extract_instagram_phone(
                "Please call me on 98765 43210",
                country_code="+91",
            ),
            "+919876543210",
        )
        self.assertEqual(
            extract_instagram_phone(
                "My budget is 10000000",
                country_code="+91",
            ),
            "",
        )
        self.assertEqual(
            extract_instagram_phone("Reach me at +14155552671", country_code="+91"),
            "+14155552671",
        )

    def test_instagram_permission_and_tenant_guard_do_not_require_phone(self):
        permission = AIPermissionService().evaluate(
            organization=self.org,
            lead=self.lead,
            channel="instagram",
        )
        self.assertTrue(permission.allowed)
        self.assertEqual(permission.reason, "allowed")
        self.assertEqual(self.lead.phone, "")

        guarded = TenantGuard(self.org).validate_message(
            self.inbound,
            lead=self.lead,
        )
        self.assertEqual(guarded.pk, self.inbound.pk)

    @patch("apps.channels.instagram_tasks.send_instagram_message_task.delay")
    @patch("apps.channels.instagram_tasks.generate_instagram_ai_engagement_task.apply_async")
    def test_recovery_republishes_stale_instagram_ai_turn(
        self,
        ai_publish,
        send_publish,
    ):
        from apps.ai_engagement.services.execution_tracker import (
            recover_api_engagement,
        )

        self.inbound.raw_payload = {
            "shvya_ai_execution": {
                "status": "queued",
                "attempts": 0,
                "updated_at": (
                    timezone.now() - timedelta(minutes=2)
                ).isoformat(),
            }
        }
        self.inbound.save(update_fields=["raw_payload", "updated_at"])

        result = recover_api_engagement()

        self.assertEqual(result["requeued"], 1)
        ai_publish.assert_called_once_with(
            args=[str(self.inbound.pk)],
            countdown=0,
        )
        send_publish.assert_not_called()
        self.inbound.refresh_from_db()
        execution = self.inbound.raw_payload["shvya_ai_execution"]
        self.assertEqual(execution["status"], "queued")
        self.assertEqual(
            execution["reason"],
            "recovered_after_dispatch_delay",
        )

    @patch("apps.channels.instagram_tasks.send_instagram_message_task.delay")
    @patch("apps.channels.instagram_tasks.generate_instagram_ai_engagement_task.apply_async")
    def test_recovery_republishes_unclaimed_queued_instagram_ai_reply(
        self,
        ai_publish,
        send_publish,
    ):
        from apps.ai_engagement.services.execution_tracker import (
            recover_api_engagement,
        )

        outbound = InstagramMessage.objects.create(
            organization=self.org,
            account=self.account,
            conversation=self.conversation,
            direction=InstagramMessage.Direction.OUTBOUND,
            status=InstagramMessage.Status.QUEUED,
            sender_id=self.account.ig_user_id,
            recipient_id=self.conversation.participant_id,
            body="Queued AI reply",
            raw_payload={
                "shvya_ai": {
                    "source_inbound_message_id": str(self.inbound.pk),
                }
            },
        )
        InstagramMessage.objects.filter(pk=outbound.pk).update(
            created_at=timezone.now() - timedelta(seconds=30),
        )

        result = recover_api_engagement()

        self.assertEqual(result["requeued"], 1)
        send_publish.assert_called_once_with(str(outbound.pk))
        ai_publish.assert_not_called()

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_inbound_instagram_turn_queues_one_ai_reply_without_phone(
        self,
        engage,
        reply_allowed,
        dispatch,
    ):
        engage.return_value = EngagementDecision(
            should_engage=True,
            message="Thanks for reaching out. How can I help?",
            file_document_id=None,
            crm_actions=[],
            reason="NORMAL_CONVERSATION",
            reason_code="NORMAL_CONVERSATION",
            model="test-model",
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = execute_instagram_ai_engagement(
                task=self.task(),
                message_id=self.inbound.pk,
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["engaged"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.phone, "")

        outbound = InstagramMessage.objects.get(
            conversation=self.conversation,
            direction=InstagramMessage.Direction.OUTBOUND,
        )
        self.assertEqual(outbound.body, "Thanks for reaching out. How can I help?")
        self.assertEqual(
            outbound.raw_payload["shvya_ai"]["source_inbound_message_id"],
            str(self.inbound.pk),
        )
        self.assertEqual(outbound.raw_payload["shvya_ai"]["provider"], "instagram")
        dispatch.assert_called_once_with(str(outbound.pk))
        self.assertGreaterEqual(reply_allowed.call_count, 2)

        self.inbound.refresh_from_db()
        self.assertTrue(
            self.inbound.raw_payload["shvya_ai_processing"]["processed"]
        )

        # If the generation task is redelivered before the send worker claims
        # the queued outbound, only the existing send is republished.
        second = execute_instagram_ai_engagement(
            task=self.task(),
            message_id=self.inbound.pk,
        )
        self.assertEqual(second["status"], "queued")
        self.assertEqual(second["reason"], "existing_ai_response_requeued")
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(
            InstagramMessage.objects.filter(
                conversation=self.conversation,
                direction=InstagramMessage.Direction.OUTBOUND,
            ).count(),
            1,
        )

        # Once Meta delivery has been claimed/sent, another redelivery is a
        # pure duplicate and does not enqueue another customer reply.
        outbound.status = InstagramMessage.Status.SENT
        outbound.save(update_fields=["status", "updated_at"])
        third = execute_instagram_ai_engagement(
            task=self.task(),
            message_id=self.inbound.pk,
        )
        self.assertEqual(third["status"], "skipped")
        self.assertEqual(third["reason"], "duplicate_ai_response")
        self.assertEqual(dispatch.call_count, 2)
