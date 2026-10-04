"""Regression coverage for the durable, shared WhatsApp AI send boundary."""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import timedelta
from io import BytesIO
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.channels.models import AIMessageSendState, WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import Lead, Pipeline
from apps.hosted_automation.models import HostedAutomationJob
from apps.organizations.models import Organization
from services.channels.ai_send_gate import (
    AIMessageDeferred,
    AI_SEND_GAP_SECONDS,
    ai_send_gap_seconds,
    AI_SEND_LEASE_SECONDS,
    _finish,
    _priority_wait,
    _reserve,
    next_ai_send_at,
    paced_ai_send,
)
from services.channels.whatsapp_service import WhatsAppSendError


class SendGateFixtures:
    def setUp(self):
        super().setUp()
        wakeup = patch("apps.hosted_automation.signals.dispatch_due_hosted_ai.apply_async")
        wakeup.start()
        self.addCleanup(wakeup.stop)
        self.organization = Organization.objects.create(name="AI Send Gate", package="dfy")
        self.org_info = OrgInfo.objects.create(organization=self.organization, ai_enabled=True)
        self.account, self.pipeline, self.lead = self._sender(1)
        self.stage = self.lead.stage
        self.now = timezone.now()

    def _sender(self, index):
        phone = f"+9190000010{index:02d}"
        pipeline = Pipeline.objects.create(
            organization=self.organization, name=f"Sender {index}",
            country_code="+91", phone_number=phone[3:], ai_enabled=True,
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type=WhatsAppAccount.ConnectionType.API,
            display_phone_number=phone, phone_number_id=f"test-sender-{index}",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )
        lead = Lead.objects.create(
            organization=self.organization, pipeline=pipeline,
            stage=pipeline.stages.get(name="New leads"),
            name=f"Lead {index}", phone=f"+9191000010{index:02d}", ai_enabled=True,
        )
        return account, pipeline, lead

    def _message(self, *, kind="reply", account=None, lead=None, job=None):
        account = account or self.account
        lead = lead or self.lead
        key = "shvya_welcome" if kind == "welcome" else "shvya_ai"
        metadata = {"origin": "bump_up" if kind == "bump_up" else kind}
        if job:
            metadata["job_id"] = str(job.pk)
        if kind == "bump_up":
            metadata["number"] = 1
        return WhatsAppMessage.objects.create(
            organization=self.organization, account=account, lead=lead,
            direction="outbound", status="queued", body=f"AI {kind}",
            from_number=account.display_phone_number, to_number=lead.phone,
            raw_payload={key: metadata},
        )

    def _job(self, *, kind="ai_engagement", created_at=None):
        job = HostedAutomationJob.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            kind=kind, available_at=self.now - timedelta(seconds=1),
        )
        if created_at is not None:
            HostedAutomationJob.objects.filter(pk=job.pk).update(created_at=created_at)
            job.refresh_from_db()
        return job

    @staticmethod
    def _provider_send(*, message):
        message.status = "sent"
        message.sent_at = timezone.now()
        message.external_id = f"mock-provider-{message.pk}"
        message.save(update_fields=["status", "sent_at", "external_id", "updated_at"])
        return message


class AIMessageSendGateTests(SendGateFixtures, TestCase):
    def test_first_send_after_upgrade_respects_recent_delivery_without_state(self):
        previous = self._message()
        WhatsAppMessage.objects.filter(pk=previous.pk).update(
            status="sent", sent_at=self.now - timedelta(seconds=max(AI_SEND_GAP_SECONDS - 2, 0)),
        )
        message = self._message()
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)
        self.assertFalse(AIMessageSendState.objects.exists())

        with patch("django.utils.timezone.now", return_value=self.now):
            for _ in range(2):
                with self.assertRaises(AIMessageDeferred) as deferred:
                    send(message=message)
                self.assertEqual(deferred.exception.available_at, self.now + timedelta(seconds=2))
                # The failed reservation rolls back state creation. Repeating
                # the check must re-seed safely instead of allowing a burst.
                self.assertFalse(AIMessageSendState.objects.exists())
        provider.assert_not_called()
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=2)):
            send(message=message)
        provider.assert_called_once()
        self.assertEqual(
            AIMessageSendState.objects.get(account=self.account).next_send_at,
            self.now + timedelta(seconds=2 + AI_SEND_GAP_SECONDS),
        )

    def test_api_replies_send_by_arrival_when_generation_finishes_out_of_order(self):
        arrival = self.now.replace(microsecond=0) - timedelta(minutes=1)
        # The later customer's model call finished first, so its outbound row
        # is older. The SQL queue annotation must still select the first turn.
        later = self._message()
        later.raw_payload["shvya_ai"]["queued_at"] = (arrival + timedelta(seconds=10)).isoformat()
        later.save(update_fields=["raw_payload"])
        earlier = self._message()
        earlier.raw_payload["shvya_ai"]["queued_at"] = arrival.isoformat()
        earlier.save(update_fields=["raw_payload"])
        self.assertLess(later.created_at, earlier.created_at)
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)

        with patch("django.utils.timezone.now", return_value=self.now):
            with self.assertRaises(AIMessageDeferred) as deferred:
                send(message=later)
            self.assertEqual(deferred.exception.reason, "ai_queue_priority")
            provider.assert_not_called()
            send(message=earlier)
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_GAP_SECONDS)):
            send(message=later)
        self.assertEqual(
            [call.kwargs["message"].pk for call in provider.call_args_list],
            [earlier.pk, later.pk],
        )

    def test_api_later_reply_waits_for_earlier_generation_until_terminal(self):
        arrival = self.now.replace(microsecond=0) - timedelta(minutes=1)
        earlier = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            direction="inbound", status="received", body="Earlier customer question",
            raw_payload={"shvya_ai_execution": {
                "status": "processing", "updated_at": self.now.isoformat(),
            }},
        )
        WhatsAppMessage.objects.filter(pk=earlier.pk).update(created_at=arrival)
        later = self._message()
        later.raw_payload["shvya_ai"]["queued_at"] = (arrival + timedelta(seconds=10)).isoformat()
        later.save(update_fields=["raw_payload"])
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)

        with self.assertRaises(AIMessageDeferred) as deferred:
            send(message=later)
        self.assertEqual(deferred.exception.reason, "ai_queue_priority")
        provider.assert_not_called()
        # A terminal turn (for example toggled off during generation) must
        # release queue priority rather than block every later customer.
        earlier.raw_payload["shvya_ai_execution"]["status"] = "skipped"
        earlier.save(update_fields=["raw_payload"])
        send(message=later)
        provider.assert_called_once()

    @override_settings(AI_ENGAGEMENT_DEBOUNCE_SECONDS=0, HOSTED_AI_REPLY_DELAY_SECONDS=0)
    def test_welcome_reply_and_bump_up_share_locked_sender_gap(self):
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)
        welcome = self._message(kind="welcome")
        reply = self._message()
        bump = self._message(kind="bump_up")
        # Explicitly enable bump-up controls; pacing remains shared across types.
        self.organization.settings = {
            "hosted_whatsapp": {"sessions": {str(self.account.pk): {
                "bump_up_messages": True, "bump_up_count": 3,
            }}}
        }
        self.organization.save(update_fields=["settings"])
        self.org_info.bump_up_enabled = True
        self.org_info.bump_up_count = 3
        self.org_info.save(update_fields=["bump_up_enabled", "bump_up_count"])

        with patch("django.utils.timezone.now", return_value=self.now):
            send(message=welcome)
            with self.assertRaises(AIMessageDeferred) as deferred:
                send(message=reply)
        self.assertEqual(deferred.exception.available_at, self.now + timedelta(seconds=AI_SEND_GAP_SECONDS))
        self.assertEqual(AI_SEND_GAP_SECONDS, 5)
        reply.refresh_from_db()
        self.assertEqual(reply.status, "queued")
        self.assertIsNone(reply.sent_at)
        self.assertEqual(provider.call_count, 1)

        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_GAP_SECONDS - 1)):
            with self.assertRaises(AIMessageDeferred):
                send(message=reply)
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_GAP_SECONDS)):
            send(message=reply)
            with self.assertRaises(AIMessageDeferred):
                send(message=bump)
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_GAP_SECONDS * 2)):
            send(message=bump)
        self.assertEqual(provider.call_count, 3)
        state = AIMessageSendState.objects.get(account=self.account)
        self.assertEqual(state.last_sent_at, self.now + timedelta(seconds=AI_SEND_GAP_SECONDS * 2))
        self.assertEqual(state.next_send_at, self.now + timedelta(seconds=AI_SEND_GAP_SECONDS * 3))

    def test_reply_defers_to_welcome_and_fifo_within_each_type(self):
        reply_job = self._job(created_at=self.now - timedelta(seconds=30))
        welcome_job = self._job(kind="welcome", created_at=self.now - timedelta(seconds=10))
        reply = self._message(job=reply_job)
        welcome = self._message(kind="welcome", job=welcome_job)
        self.assertIsNotNone(_priority_wait(reply, self.now))
        self.assertIsNone(_priority_wait(welcome, self.now))
        HostedAutomationJob.objects.filter(pk=welcome_job.pk).update(status="completed")
        WhatsAppMessage.objects.filter(pk=welcome.pk).update(status="sent")
        next_reply_job = self._job(created_at=self.now - timedelta(seconds=5))
        next_reply = self._message(job=next_reply_job)
        self.assertIsNone(_priority_wait(reply, self.now))
        self.assertIsNotNone(_priority_wait(next_reply, self.now))

    def test_legacy_message_and_durable_job_cannot_wait_on_each_other(self):
        for kind, job_kind in [("reply", "ai_engagement"), ("welcome", "welcome")]:
            with self.subTest(kind=kind):
                HostedAutomationJob.objects.all().delete()
                WhatsAppMessage.objects.all().delete()
                legacy = self._message(kind=kind)
                WhatsAppMessage.objects.filter(pk=legacy.pk).update(
                    created_at=self.now - timedelta(seconds=30),
                )
                legacy.refresh_from_db()
                job = self._job(kind=job_kind, created_at=self.now - timedelta(seconds=10))
                current = self._message(kind=kind, job=job)
                self.assertIsNone(_priority_wait(legacy, self.now))
                self.assertIsNotNone(_priority_wait(current, self.now))

    def test_oldest_durable_job_precedes_later_legacy_message(self):
        job = self._job(created_at=self.now - timedelta(seconds=20))
        current = self._message(job=job)
        legacy = self._message()
        self.assertIsNone(_priority_wait(current, self.now))
        self.assertIsNotNone(_priority_wait(legacy, self.now))

    def test_stale_duplicate_and_terminal_rows_never_reach_provider(self):
        message = self._message()
        stale = WhatsAppMessage.objects.get(pk=message.pk)
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)
        send(message=message)
        send(message=stale)
        self.assertEqual(provider.call_count, 1)
        for status in ("delivered", "read", "failed"):
            with self.subTest(status=status):
                WhatsAppMessage.objects.filter(pk=message.pk).update(status=status)
                if status == "failed":
                    with self.assertRaises(WhatsAppSendError):
                        send(message=stale)
                else:
                    send(message=stale)
        self.assertEqual(provider.call_count, 1)

    def test_stale_object_loaded_before_ai_marker_cannot_bypass_live_toggles(self):
        message = self._message()
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload={})
        stale = WhatsAppMessage.objects.get(pk=message.pk)
        WhatsAppMessage.objects.filter(pk=message.pk).update(
            raw_payload={"shvya_welcome": {"trigger": "lead_created"}},
        )
        Lead.objects.filter(pk=self.lead.pk).update(ai_enabled=False)
        provider = Mock(side_effect=self._provider_send)
        with self.assertRaisesMessage(WhatsAppSendError, "lead_ai_disabled"):
            paced_ai_send(provider)(message=stale)
        provider.assert_not_called()

    def test_worker_paused_before_reservation_cannot_resend_completed_row(self):
        message = self._message()
        provider = Mock(side_effect=self._provider_send)

        def resume_after_another_worker_sent(message):
            # Model a worker descheduled after its initial read while another
            # worker sent successfully and its configured conversational cooldown elapsed.
            WhatsAppMessage.objects.filter(pk=message.pk).update(
                status="sent", sent_at=self.now - timedelta(seconds=AI_SEND_GAP_SECONDS + 1),
                external_id="other-worker-provider-id",
            )
            AIMessageSendState.objects.create(
                account=self.account, next_send_at=self.now - timedelta(seconds=1),
                last_sent_at=self.now - timedelta(seconds=AI_SEND_GAP_SECONDS + 1),
            )
            return _reserve(message)

        with patch("services.channels.ai_send_gate._reserve", side_effect=resume_after_another_worker_sent):
            result = paced_ai_send(provider)(message=message)
        provider.assert_not_called()
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.external_id, "other-worker-provider-id")

    def test_live_toggles_cancel_previously_queued_welcome(self):
        toggles = [
            (OrgInfo, self.org_info.pk, "ai_enabled", "organization_ai_disabled"),
            (Pipeline, self.pipeline.pk, "ai_enabled", "pipeline_ai_disabled"),
            (type(self.stage), self.stage.pk, "ai_on", "stage_ai_disabled"),
            (Lead, self.lead.pk, "ai_enabled", "lead_ai_disabled"),
        ]
        provider = Mock(side_effect=self._provider_send)
        send = paced_ai_send(provider)
        for model, pk, field, reason in toggles:
            with self.subTest(toggle=reason):
                message = self._message(kind="welcome")
                # Prime related objects as a long-running Celery task would.
                self.assertTrue(message.lead.ai_enabled)
                self.assertTrue(message.lead.stage.ai_on)
                model.objects.filter(pk=pk).update(**{field: False})
                with self.assertRaisesMessage(WhatsAppSendError, reason):
                    send(message=message)
                message.refresh_from_db()
                self.assertEqual(message.status, "failed")
                self.assertIsNone(message.sent_at)
                model.objects.filter(pk=pk).update(**{field: True})
        provider.assert_not_called()
        self.assertFalse(AIMessageSendState.objects.exists())

    def test_qualified_stage_reply_is_allowed_when_toggles_are_on(self):
        qualified = self.pipeline.stages.get(name="Qualified")
        qualified.ai_on = True
        qualified.save(update_fields=["ai_on"])
        Lead.objects.filter(pk=self.lead.pk).update(stage=qualified)
        provider = Mock(side_effect=self._provider_send)
        message = self._message()
        paced_ai_send(provider)(message=message)
        provider.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.status, "sent")

    def test_fairness_deferral_does_not_extend_send_cooldown(self):
        message = self._message()
        provider = Mock()
        due = self.now + timedelta(seconds=2)
        with patch("django.utils.timezone.now", return_value=self.now), patch(
            "services.channels.ai_send_gate._admit_ai_provider",
            side_effect=AIMessageDeferred(due, "whatsapp_account_fairness_limit"),
        ):
            with self.assertRaises(AIMessageDeferred):
                paced_ai_send(provider)(message=message)
        state = AIMessageSendState.objects.get(account=self.account)
        self.assertIsNone(state.claimed_until)
        self.assertIsNone(state.next_send_at)
        provider.assert_not_called()

    def test_failed_attempt_keeps_cooldown_without_recording_sent_message(self):
        message = self._message()
        provider = Mock(side_effect=WhatsAppSendError("Provider unavailable"))
        with patch("django.utils.timezone.now", return_value=self.now):
            with self.assertRaisesMessage(WhatsAppSendError, "Provider unavailable"):
                paced_ai_send(provider)(message=message)
        state = AIMessageSendState.objects.get(account=self.account)
        self.assertIsNone(state.last_sent_at)
        self.assertIsNone(state.claim_token)
        self.assertEqual(state.next_send_at, self.now + timedelta(seconds=AI_SEND_GAP_SECONDS))
        message.refresh_from_db()
        self.assertIsNone(message.sent_at)

    def test_crashed_worker_lease_is_durable_and_blocks_immediate_takeover(self):
        message = self._message()
        with patch("django.utils.timezone.now", return_value=self.now):
            _reserve(message)
            with self.assertRaises(AIMessageDeferred):
                _reserve(message)
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertEqual(next_ai_send_at(self.account), self.now + timedelta(seconds=5))
        safe_takeover = self.now + timedelta(seconds=AI_SEND_LEASE_SECONDS + AI_SEND_GAP_SECONDS)
        self.assertEqual(AIMessageSendState.objects.get(account=self.account).next_send_at, safe_takeover)
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_LEASE_SECONDS + 1)):
            with self.assertRaises(AIMessageDeferred) as deferred:
                _reserve(message)
        self.assertEqual(deferred.exception.available_at, safe_takeover)

    def test_active_sender_followers_poll_then_use_actual_completion_gap(self):
        first = self._message()
        follower = self._message()
        with patch("django.utils.timezone.now", return_value=self.now):
            token = _reserve(first)
            with self.assertRaises(AIMessageDeferred) as deferred:
                _reserve(follower)
            self.assertEqual(deferred.exception.available_at, self.now + timedelta(seconds=5))
            self.assertEqual(next_ai_send_at(self.account), self.now + timedelta(seconds=5))
            self._provider_send(message=first)
            _finish(first, token)
            self.assertEqual(next_ai_send_at(self.account), self.now + timedelta(seconds=AI_SEND_GAP_SECONDS))
            with self.assertRaises(AIMessageDeferred) as deferred:
                _reserve(follower)
            self.assertEqual(deferred.exception.available_at, self.now + timedelta(seconds=AI_SEND_GAP_SECONDS))

    def test_manual_message_is_not_subject_to_ai_toggle_or_cooldown(self):
        message = self._message()
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload={})
        message.refresh_from_db()
        Lead.objects.filter(pk=self.lead.pk).update(ai_enabled=False)
        provider = Mock(side_effect=self._provider_send)
        paced_ai_send(provider)(message=message)
        provider.assert_called_once()
        self.assertFalse(AIMessageSendState.objects.exists())

    def test_each_actual_transport_rechecks_stage_before_provider_io(self):
        from services.channels.hosted_whatsapp_transport import send_hosted_message
        from services.channels.whatsapp_service import send_outbound_message
        from services.channels.whatsapp_template_delivery import _send_template_transport

        type(self.stage).objects.filter(pk=self.stage.pk).update(ai_on=False)
        for sender in (send_hosted_message, send_outbound_message, _send_template_transport):
            with self.subTest(sender=sender.__module__):
                message = self._message(kind="welcome")
                with patch("services.channels.whatsapp_service.WhatsAppClient") as meta:
                    with patch("apps.channels.hosted_gateway_routing.gateway_client_for_account") as hosted:
                        with self.assertRaisesMessage(WhatsAppSendError, "stage_ai_disabled"):
                            sender(message=message)
                meta.assert_not_called()
                hosted.assert_not_called()

    def test_template_and_meta_text_transports_share_actual_send_cooldown(self):
        from services.channels.whatsapp_service import send_outbound_message

        self.account.access_token = "test-token"
        self.account.save(update_fields=["access_token"])
        template = WhatsAppTemplate.objects.create(
            organization=self.organization, account=self.account, name="test_welcome",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Welcome", meta_template_id="test-approved-template",
        )
        welcome = self._message(kind="welcome")
        welcome.media_payload = {
            "transport": "template", "template_id": str(template.pk), "template_name": template.name,
            "language_code": "en_US", "components": [],
        }
        welcome.save(update_fields=["media_payload"])
        reply = self._message()
        with patch("services.channels.whatsapp_service.WhatsAppClient") as client_class:
            client = client_class.return_value
            client.send_template_message.return_value = {"messages": [{"id": "mock-template-id"}]}
            client.send_text_message.return_value = {"messages": [{"id": "mock-text-id"}]}
            with patch("django.utils.timezone.now", return_value=self.now):
                send_outbound_message(message=welcome)
                with self.assertRaises(AIMessageDeferred):
                    send_outbound_message(message=reply)
            client.send_template_message.assert_called_once()
            client.send_text_message.assert_not_called()
            with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=AI_SEND_GAP_SECONDS)):
                send_outbound_message(message=reply)
            client.send_text_message.assert_called_once()
        welcome.refresh_from_db()
        reply.refresh_from_db()
        self.assertEqual(welcome.status, "sent")
        self.assertEqual(reply.sent_at - welcome.sent_at, timedelta(seconds=AI_SEND_GAP_SECONDS))

    def test_hosted_welcome_can_send_without_existing_inbound_conversation(self):
        from services.channels.hosted_whatsapp_transport import send_hosted_message

        self.account.connection_type = WhatsAppAccount.ConnectionType.coexisted
        self.account.save(update_fields=["connection_type"])
        welcome = self._message(kind="welcome")
        self.assertFalse(WhatsAppMessage.objects.filter(direction="inbound").exists())
        with (
            patch("apps.channels.hosted_gateway_routing.gateway_client_for_account") as gateway,
            patch("services.channels.hosted_health_guard.reserve_hosted_automation_send", return_value={"reserved": True}),
            patch("services.channels.hosted_health_guard.finalize_hosted_send"),
            patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh"),
        ):
            gateway.return_value.send_message.return_value = {"messageId": "mock-hosted-welcome"}
            send_hosted_message(message=welcome)
            gateway.return_value.send_message.assert_called_once()
        welcome.refresh_from_db()
        self.assertEqual(welcome.status, "sent")
        self.assertTrue(AIMessageSendState.objects.filter(account=self.account, last_sent_at__isnull=False).exists())


class LegacyHostedSendGateTests(SendGateFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.account.connection_type = WhatsAppAccount.ConnectionType.coexisted
        self.account.save(update_fields=["connection_type"])
        patches = {
            "gateway": patch("apps.channels.hosted_gateway_routing.gateway_client_for_account"),
            "health": patch("services.channels.hosted_health_guard.reserve_hosted_automation_send", return_value={"reserved": True}),
            "finalize": patch("services.channels.hosted_health_guard.finalize_hosted_send"),
            "refresh": patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh"),
            "fairness": patch("apps.core.fairness.admit_provider_start", return_value=(True, None, "account")),
        }
        for name, patcher in patches.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.gateway.return_value.send_message.return_value = {"messageId": "mock-legacy-welcome"}

    def test_legacy_eta_welcomes_share_gate_and_record_actual_sent_time(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        first, second = self._message(kind="welcome"), self._message(kind="welcome")
        with patch("django.utils.timezone.now", return_value=self.now):
            sent = send_hosted_whatsapp_message_task.run(str(first.pk))
            with patch.object(send_hosted_whatsapp_message_task, "apply_async") as requeue:
                deferred = send_hosted_whatsapp_message_task.run(str(second.pk))
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["reason"], "ai_send_gap")
        requeue.assert_called_once_with(args=[str(second.pk)], eta=self.now + timedelta(seconds=ai_send_gap_seconds(self.account)))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.sent_at, self.now)
        self.assertEqual(second.status, "queued")
        self.assertIsNone(second.sent_at)
        self.gateway.return_value.send_message.assert_called_once()
        self.gateway.return_value.send_message.return_value = {"messageId": "mock-second-welcome"}
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=ai_send_gap_seconds(self.account))):
            self.assertEqual(send_hosted_whatsapp_message_task.run(str(second.pk))["status"], "sent")
        second.refresh_from_db()
        self.assertEqual(second.sent_at - first.sent_at, timedelta(seconds=ai_send_gap_seconds(self.account)))

    def test_legacy_welcome_respects_stage_toggle_changed_after_queueing(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        welcome = self._message(kind="welcome")
        type(self.stage).objects.filter(pk=self.stage.pk).update(ai_on=False)
        result = send_hosted_whatsapp_message_task.run(str(welcome.pk))
        self.assertEqual(result["status"], "failed")
        self.assertIn("stage_ai_disabled", result["error"])
        self.gateway.assert_not_called()

    def test_legacy_eta_does_not_take_a_durable_job_owned_message(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        job = self._job(kind="welcome")
        welcome = self._message(kind="welcome", job=job)
        result = send_hosted_whatsapp_message_task.run(str(welcome.pk))
        self.assertEqual(result, {"status": "skipped", "reason": "durable_ai_job_owned"})
        self.gateway.assert_not_called()
        welcome.refresh_from_db()
        self.assertEqual(welcome.status, "queued")

    def test_storage_upload_uses_canonical_transport_and_stable_request_identity(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self._message(kind="welcome")
        message.message_type = WhatsAppMessage.MessageType.DOCUMENT
        message.media_payload = {
            "source": "storage", "storage_path": "test-uploads/welcome.pdf",
            "mime_type": "application/pdf", "filename": "welcome.pdf",
        }
        message.save(update_fields=["message_type", "media_payload"])
        self.gateway.return_value.send_uploaded_media.return_value = {"messageId": "mock-upload-id"}
        with (
            patch("django.core.files.storage.default_storage.open", return_value=BytesIO(b"test document")) as read,
            patch("django.core.files.storage.default_storage.delete") as cleanup,
        ):
            result = send_hosted_whatsapp_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "sent")
        read.assert_called_once_with("test-uploads/welcome.pdf", "rb")
        cleanup.assert_called_once_with("test-uploads/welcome.pdf")
        args = self.gateway.return_value.send_uploaded_media.call_args.kwargs
        self.assertEqual(args["request_id"], str(message.pk))
        self.assertFalse(args["request_is_retry"])
        self.assertEqual(args["filename"], "welcome.pdf")
        message.refresh_from_db()
        self.assertIsNotNone(message.sent_at)
        self.assertEqual(message.external_id, "wweb:mock-upload-id")

    def test_storage_upload_is_retained_while_ai_send_is_deferred(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self._message(kind="welcome")
        message.message_type = WhatsAppMessage.MessageType.DOCUMENT
        message.media_payload = {"source": "storage", "storage_path": "test-uploads/welcome.pdf"}
        message.save(update_fields=["message_type", "media_payload"])
        AIMessageSendState.objects.create(account=self.account, next_send_at=self.now + timedelta(seconds=ai_send_gap_seconds(self.account)))
        with (
            patch.object(send_hosted_whatsapp_message_task, "apply_async"),
            patch("django.core.files.storage.default_storage.open") as read,
            patch("django.core.files.storage.default_storage.delete") as cleanup,
        ):
            result = send_hosted_whatsapp_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "deferred")
        read.assert_not_called()
        cleanup.assert_not_called()
        self.gateway.assert_not_called()

    def test_missing_provider_id_is_failed_without_fabricated_send_timestamp(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self._message(kind="welcome")
        self.gateway.return_value.send_message.return_value = {}
        result = send_hosted_whatsapp_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "failed")
        message.refresh_from_db()
        self.assertEqual(message.status, "failed")
        self.assertIsNone(message.sent_at)

    def test_gateway_replay_preserves_original_send_time_and_retry_identity(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self._message(kind="welcome")
        original_send = self.now - timedelta(minutes=2)
        message.raw_payload["shvya_hosted_request"] = {
            "request_id": str(message.pk), "attempted_at": original_send.isoformat(),
        }
        message.save(update_fields=["raw_payload"])
        self.gateway.return_value.send_message.return_value = {
            "messageId": "mock-recovered-send", "timestamp": original_send.timestamp(),
            "idempotencyReplay": True,
        }
        with patch("django.utils.timezone.now", return_value=self.now):
            result = send_hosted_whatsapp_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "sent")
        message.refresh_from_db()
        self.assertEqual(message.sent_at, original_send)
        state = AIMessageSendState.objects.get(account=self.account)
        self.assertEqual(state.last_sent_at, original_send)
        self.assertEqual(state.next_send_at, original_send + timedelta(seconds=ai_send_gap_seconds(self.account)))
        request = self.gateway.return_value.send_message.call_args.kwargs
        self.assertEqual(request["request_id"], str(message.pk))
        self.assertTrue(request["request_is_retry"])

    def test_uncertain_or_conflicting_gateway_claim_is_not_retried(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task
        from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError

        for code in ("provider_outcome_uncertain", "request_payload_conflict"):
            with self.subTest(code=code):
                AIMessageSendState.objects.all().delete()
                message = self._message(kind="welcome")
                self.gateway.return_value.send_message.side_effect = WhatsAppWebGatewayError(
                    "Automatic replay blocked", status_code=409,
                    response_body='{"code": "' + code + '"}',
                )
                with patch.object(send_hosted_whatsapp_message_task, "retry") as retry:
                    result = send_hosted_whatsapp_message_task.run(str(message.pk))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], code)
                retry.assert_not_called()
                message.refresh_from_db()
                self.assertEqual(message.status, "failed")

    def test_waiting_legacy_tasks_do_not_consume_provider_fairness_capacity(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self._message(kind="welcome")
        AIMessageSendState.objects.create(account=self.account, next_send_at=self.now + timedelta(seconds=ai_send_gap_seconds(self.account)))
        with patch.object(send_hosted_whatsapp_message_task, "apply_async"):
            result = send_hosted_whatsapp_message_task.run(str(message.pk))
        self.assertEqual(result["status"], "deferred")
        self.fairness.assert_not_called()
        self.gateway.assert_not_called()


@skipUnlessDBFeature("has_select_for_update_skip_locked")
class AIMessageSendGateConcurrencyTests(SendGateFixtures, TransactionTestCase):
    def _parallel_sends(self, messages, *, start_oldest_first=False):
        """Use distinct PostgreSQL connections; never invoke an external provider."""
        barrier = Barrier(len(messages) - int(start_oldest_first))
        provider_entered = Event()
        release_provider = Event()
        calls_lock = Lock()
        calls = []

        def provider(*, message):
            self.assertFalse(connection.in_atomic_block)
            with calls_lock:
                calls.append(message.pk)
            provider_entered.set()
            self.assertTrue(release_provider.wait(timeout=10))
            return self._provider_send(message=message)

        send = paced_ai_send(provider)

        def worker(message_id, *, synchronize=True):
            close_old_connections()
            try:
                message = WhatsAppMessage.objects.get(pk=message_id)
                if synchronize:
                    barrier.wait(timeout=10)
                try:
                    send(message=message)
                    return "sent"
                except AIMessageDeferred:
                    return "deferred"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(messages)) as pool:
            if start_oldest_first:
                # Begin the oldest message's provider I/O, then race every
                # remaining lead against that live, committed reservation.
                futures = [pool.submit(worker, messages[0].pk, synchronize=False)]
                self.assertTrue(provider_entered.wait(timeout=10))
                futures += [pool.submit(worker, message.pk) for message in messages[1:]]
            else:
                futures = [pool.submit(worker, message.pk) for message in messages]
            try:
                self.assertTrue(provider_entered.wait(timeout=10))
                # The claim must be committed and visible while provider I/O is running.
                self.assertTrue(AIMessageSendState.objects.filter(claim_token__isnull=False).exists())
                wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            finally:
                release_provider.set()
            results = [future.result(timeout=15) for future in futures]
        return calls, results

    def test_simultaneous_leads_on_one_number_admit_only_one_provider_send(self):
        self.assertEqual(connection.vendor, "postgresql")
        messages = [self._message() for _ in range(8)]
        calls, results = self._parallel_sends(messages, start_oldest_first=True)
        self.assertEqual(calls, [messages[0].pk])
        self.assertEqual(results.count("sent"), 1)
        self.assertEqual(results.count("deferred"), 7)
        self.assertEqual(WhatsAppMessage.objects.filter(status="sent").count(), 1)
        self.assertEqual(WhatsAppMessage.objects.filter(status="queued").count(), 7)

    def test_duplicate_workers_for_same_message_cannot_both_send(self):
        message = self._message()
        calls, _ = self._parallel_sends([message, message])
        self.assertEqual(calls, [message.pk])

    def test_two_accounts_send_independently_without_global_lock(self):
        account, _, lead = self._sender(2)
        messages = [self._message(), self._message(account=account, lead=lead)]
        calls, results = self._parallel_sends(messages)
        self.assertCountEqual(calls, [message.pk for message in messages])
        self.assertEqual(results, ["sent", "sent"])
        self.assertEqual(AIMessageSendState.objects.count(), 2)
