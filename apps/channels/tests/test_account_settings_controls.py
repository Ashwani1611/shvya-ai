"""Regression coverage for connected API and Hosted account gear controls."""

from datetime import datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.ai_engagement.models import OrgInfo
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
)
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import (
    _persist_gateway_message,
    get_session_settings,
    update_session_settings,
)
from services.channels.whatsapp_service import (
    WhatsAppSendError,
    handle_inbound_message,
    queue_outbound_message,
    resolve_account_for_lead,
    send_outbound_message,
)
from services.followup_service import (
    assign_sequence,
    process_due_state,
    register_lead_reply,
    register_manual_outbound,
)

UTC = dt_timezone.utc


class AccountControlsMixin:
    provider = "api"

    def setUp(self):
        self.now = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)  # 12:00 Kolkata
        self.clock_patcher = patch("django.utils.timezone.now", return_value=self.now)
        self.clock = self.clock_patcher.start()
        self.addCleanup(self.clock_patcher.stop)
        self.org = Organization.objects.create(name="Account controls", timezone="Asia/Kolkata")
        self.user = User.objects.create_user(
            email=f"controls-{self.provider}@example.com", password="test-password",
            organization=self.org, name="Admin", role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org, owner=self.user, name="Main pipeline",
            phone_number="+919999999991", ai_enabled=True,
        )
        self.stage, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, display_order=1, defaults={"name": "New Lead"},
        )
        self.stage.ai_on = True
        self.stage.save(update_fields=["ai_on", "updated_at"])
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type=self.provider, business_name="Main number",
            phone_number_id="api-main" if self.provider == "api" else "+919999999991",
            display_phone_number="+919999999991", status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        OrgInfo.objects.update_or_create(
            organization=self.org,
            defaults={"ai_enabled": True, "bump_up_enabled": True, "bump_up_count": 3},
        )
        AutoFollowupSettings.objects.update_or_create(
            organization=self.org,
            defaults={"enabled": True, "business_hours_start": time(0), "business_hours_end": time(23, 59)},
        )
        self.save_settings(
            ai_auto_reply=True, auto_lead_creation=True, bump_up_messages=True,
            bump_up_count=3, auto_follow_up=True,
            business_hours_start="00:00", business_hours_end="00:00",
            active_conversation_delay_value=2, active_conversation_delay_unit="hours",
        )
        self.lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Existing lead", phone="+919111111111", ai_enabled=True,
        )

    def save_settings(self, **payload):
        with self.captureOnCommitCallbacks(execute=True):
            return update_session_settings(account=self.account, payload=payload)

    def sequence(self):
        sequence = FollowupSequence.objects.create(
            organization=self.org, name=f"Sequence {FollowupSequence.objects.count()}",
            whatsapp_account=self.account, created_by=self.user,
        )
        step = FollowupStep.objects.create(
            sequence=sequence, position=1, step_type=FollowupStep.StepType.REMINDER,
            reminder_text="Call this lead", schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        state = assign_sequence(lead=self.lead, sequence=sequence, actor=self.user)
        return sequence, step, state

    def process(self, state):
        if self.provider == "hosted":
            from services.channels.hosted_automation_service import process_hosted_due_state
            return process_hosted_due_state(state.pk)
        return process_due_state(state.pk)

    def inbound(self, phone, identity):
        if self.provider == "hosted":
            return _persist_gateway_message(
                account=self.account,
                payload={
                    "messageId": identity, "from": phone, "contactPhoneNumber": phone,
                    "chatId": phone.lstrip("+") + "@c.us", "contactName": "Prospect",
                    "body": "Hello", "fromMe": False, "isGroup": False,
                },
            )
        return handle_inbound_message(
            organization=self.org, account=self.account, external_id=identity,
            from_number=phone.lstrip("+"), to_number=self.account.display_phone_number,
            body="Hello", raw_payload={},
        )

    def outbound(self, ai=None):
        if ai is not None and not self.lead.whatsapp_messages.filter(direction="inbound").exists():
            self.inbound(self.lead.phone, "source-for-outbound")
        message = queue_outbound_message(
            organization=self.org, account=self.account, lead=self.lead,
            to_number=self.lead.phone, body="Hello from the team",
        )
        if ai is not None:
            message.raw_payload = {"shvya_ai": ai}
            message.save(update_fields=["raw_payload", "updated_at"])
        return message

    def _move_lead_to_other_pipeline(self):
        other = Pipeline.objects.create(
            organization=self.org,
            owner=self.user,
            name="Other pipeline",
            phone_number="+919999999992",
            ai_enabled=True,
        )
        stage, _ = Stage.objects.get_or_create(
            pipeline=other,
            display_order=1,
            defaults={"name": "New Lead"},
        )
        self.lead.pipeline = other
        self.lead.stage = stage
        self.lead.save(update_fields=["pipeline", "stage", "updated_at"])
        self.lead.refresh_from_db()
        return other

    def test_resolver_does_not_keep_previous_number_after_pipeline_move(self):
        self.outbound()
        self._move_lead_to_other_pipeline()

        self.assertIsNone(
            resolve_account_for_lead(
                organization=self.org,
                lead=self.lead,
            )
        )

    def test_queue_blocks_number_from_previous_pipeline(self):
        self._move_lead_to_other_pipeline()

        with self.assertRaisesMessage(
            WhatsAppSendError,
            "current pipeline is linked to a different WhatsApp number",
        ):
            queue_outbound_message(
                organization=self.org,
                account=self.account,
                lead=self.lead,
                to_number=self.lead.phone,
                body="Must not cross pipelines",
            )

    def test_all_four_toggles_persist_false_and_true(self):
        keys = ("ai_auto_reply", "auto_lead_creation", "bump_up_messages", "auto_follow_up")
        for enabled in (False, True):
            self.save_settings(**dict.fromkeys(keys, enabled))
            reloaded = WhatsAppAccount.objects.select_related("organization").get(pk=self.account.pk)
            actual = get_session_settings(account=reloaded)
            for key in keys:
                self.assertEqual(actual[key], enabled, key)
            self.pipeline.refresh_from_db()
            self.assertEqual(self.pipeline.ai_enabled, enabled)

    def test_cached_account_cannot_keep_stale_settings(self):
        cached = WhatsAppAccount.objects.select_related("organization").get(pk=self.account.pk)
        self.assertTrue(get_session_settings(account=cached)["auto_follow_up"])
        self.save_settings(auto_follow_up=False, auto_lead_creation=False, bump_up_messages=False)
        actual = get_session_settings(account=cached)
        for key in ("auto_follow_up", "auto_lead_creation", "bump_up_messages"):
            self.assertFalse(actual[key], key)

    def test_other_connected_number_is_not_changed(self):
        Pipeline.objects.create(
            organization=self.org, owner=self.user, name="Second pipeline",
            phone_number="+919999999992", ai_enabled=True,
        )
        other = WhatsAppAccount.objects.create(
            organization=self.org, connection_type=self.provider,
            phone_number_id="api-second" if self.provider == "api" else "+919999999992",
            display_phone_number="+919999999992", status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        before = get_session_settings(account=other)
        self.save_settings(ai_auto_reply=False, auto_follow_up=False, auto_lead_creation=False)
        self.assertEqual(get_session_settings(account=other), before)

    def test_auto_lead_creation_off_keeps_message_without_new_lead(self):
        self.save_settings(auto_lead_creation=False)
        message = self.inbound("+919222222222", "new-off")
        self.assertIsNotNone(message)
        self.assertIsNone(message.lead_id)
        self.assertFalse(Lead.objects.filter(organization=self.org, phone="+919222222222").exists())

    def test_auto_lead_creation_on_creates_exactly_one_lead(self):
        self.save_settings(auto_lead_creation=True)
        first = self.inbound("+919222222222", "new-on")
        second = self.inbound("+919222222222", "new-on")
        self.assertIsNotNone(first.lead_id)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Lead.objects.filter(organization=self.org, phone="+919222222222").count(), 1)

    def test_creation_off_still_attaches_existing_lead(self):
        self.save_settings(auto_lead_creation=False)
        message = self.inbound(self.lead.phone, "existing-off")
        self.assertEqual(message.lead_id, self.lead.pk)

    def test_queued_ai_is_blocked_when_switch_is_turned_off(self):
        message = self.outbound({"origin": "engagement"})
        self.save_settings(ai_auto_reply=False)
        with patch("apps.channels.providers.whatsapp.WhatsAppClient.send_text_message") as meta, patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message"
        ) as hosted:
            with self.assertRaises(WhatsAppSendError):
                send_outbound_message(message=message)
        meta.assert_not_called()
        hosted.assert_not_called()

    def test_queued_bump_is_blocked_when_bump_switch_is_turned_off(self):
        message = self.outbound({"origin": "bump_up", "number": 1})
        self.save_settings(bump_up_messages=False)
        with patch("apps.channels.providers.whatsapp.WhatsAppClient.send_text_message") as meta, patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message"
        ) as hosted:
            with self.assertRaises(WhatsAppSendError):
                send_outbound_message(message=message)
        meta.assert_not_called()
        hosted.assert_not_called()

    def test_lowered_bump_limit_blocks_already_queued_extra_bump(self):
        message = self.outbound({"origin": "bump_up", "number": 3})
        self.save_settings(bump_up_count=1)
        with self.assertRaisesMessage(WhatsAppSendError, "bump_up_limit_reached"):
            send_outbound_message(message=message)

    def test_ai_message_sends_when_all_current_controls_allow_it(self):
        message = self.outbound({"origin": "engagement"})
        with patch(
            "apps.channels.providers.whatsapp.WhatsAppClient.send_text_message",
            return_value={"messages": [{"id": "enabled-api"}]},
        ) as meta, patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message",
            return_value={"messageId": "enabled-hosted"},
        ) as hosted, patch("services.channels.hosted_whatsapp_transport._push_chat_refresh"):
            send_outbound_message(message=message)
        (hosted if self.provider == "hosted" else meta).assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)

    def test_manual_message_is_not_disabled_by_automation_switches(self):
        message = self.outbound()
        self.save_settings(ai_auto_reply=False, bump_up_messages=False, auto_follow_up=False)
        with patch(
            "apps.channels.providers.whatsapp.WhatsAppClient.send_text_message",
            return_value={"messages": [{"id": "manual-api"}]},
        ), patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message",
            return_value={"messageId": "manual-hosted"},
        ), patch("services.channels.hosted_whatsapp_transport._push_chat_refresh"):
            send_outbound_message(message=message)
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)

    def test_followup_off_then_on_preserves_progress(self):
        self.save_settings(auto_follow_up=False)
        _, _, state = self.sequence()
        self.assertFalse(self.process(state))
        self.assertFalse(FollowupExecution.objects.filter(state=state).exists())
        self.save_settings(auto_follow_up=True)
        state.refresh_from_db()
        self.assertEqual(state.last_completed_position, 0)
        self.assertTrue(self.process(state))
        self.assertEqual(FollowupExecution.objects.filter(state=state).count(), 1)
        self.assertFalse(self.process(state))
        self.assertEqual(FollowupExecution.objects.filter(state=state).count(), 1)

    def test_account_hours_override_retired_organization_hours(self):
        AutoFollowupSettings.objects.filter(organization=self.org).update(
            business_hours_start=time(8), business_hours_end=time(9),
        )
        self.save_settings(business_hours_start="10:00", business_hours_end="18:00")
        _, _, state = self.sequence()
        self.assertTrue(self.process(state))

    def test_outside_hours_waits_for_next_opening_in_organization_timezone(self):
        self.save_settings(business_hours_start="09:00", business_hours_end="18:00")
        _, _, state = self.sequence()
        self.clock.return_value = datetime(2026, 9, 18, 13, 30, tzinfo=UTC)  # 19:00
        self.assertFalse(self.process(state))
        state.refresh_from_db()
        self.assertEqual(state.upcoming_send_at, datetime(2026, 9, 19, 3, 30, tzinfo=UTC))
        self.assertFalse(FollowupExecution.objects.filter(state=state).exists())

    def test_overnight_hours_send_after_midnight_and_block_in_daytime(self):
        self.save_settings(business_hours_start="20:00", business_hours_end="06:00")
        _, _, state = self.sequence()
        self.assertFalse(self.process(state))
        self.clock.return_value = datetime(2026, 9, 18, 19, 30, tzinfo=UTC)  # next day 01:00
        self.assertTrue(self.process(state))

    def test_live_delay_change_is_checked_even_without_reschedule_callback(self):
        _, _, state = self.sequence()
        replied_at = self.now - timedelta(minutes=10)
        self.save_settings(active_conversation_delay_value=5, active_conversation_delay_unit="minutes")
        register_lead_reply(lead=self.lead, at=replied_at)
        self.org.refresh_from_db()
        settings = self.org.settings
        settings["hosted_whatsapp"]["sessions"][str(self.account.pk)].update(
            active_conversation_delay_value=1, active_conversation_delay_unit="hours",
        )
        Organization.objects.filter(pk=self.org.pk).update(settings=settings)
        self.assertFalse(self.process(state))
        state.refresh_from_db()
        self.assertEqual(state.upcoming_send_at, replied_at + timedelta(hours=1))
        self.assertFalse(FollowupExecution.objects.filter(state=state).exists())

    def test_shorter_delay_recalculates_existing_wait_without_replaying(self):
        _, _, state = self.sequence()
        replied_at = self.now - timedelta(minutes=10)
        register_lead_reply(lead=self.lead, at=replied_at)
        state.refresh_from_db()
        self.assertGreater(state.upcoming_send_at, self.now)
        self.save_settings(active_conversation_delay_value=5, active_conversation_delay_unit="minutes")
        state.refresh_from_db()
        self.assertEqual(state.last_completed_position, 0)
        self.assertLessEqual(state.upcoming_send_at, self.now)
        self.assertTrue(self.process(state))
        self.assertEqual(FollowupExecution.objects.filter(state=state).count(), 1)

    def test_agent_activity_resets_delay_and_then_business_hours_apply(self):
        self.save_settings(
            business_hours_start="09:00", business_hours_end="18:00",
            active_conversation_delay_value=2, active_conversation_delay_unit="hours",
        )
        _, _, state = self.sequence()
        self.clock.return_value = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)  # 17:30
        register_manual_outbound(lead=self.lead, at=self.clock.return_value)
        self.assertFalse(self.process(state))
        state.refresh_from_db()
        self.assertEqual(state.upcoming_send_at, datetime(2026, 9, 19, 3, 30, tzinfo=UTC))

    def test_setting_change_does_not_bypass_retry_backoff(self):
        sequence, step, state = self.sequence()
        retry_at = self.now + timedelta(hours=4)
        FollowupExecution.objects.create(
            organization=self.org, state=state, lead=self.lead, sequence=sequence, step=step,
            scheduled_for=self.now, status=FollowupExecution.Status.RETRY_WAIT,
            attempt_no=1, max_attempts=2, next_retry_at=retry_at,
        )
        state.upcoming_send_at = retry_at
        state.save(update_fields=["upcoming_send_at", "updated_at"])
        self.save_settings(active_conversation_delay_value=1, active_conversation_delay_unit="minutes")
        state.refresh_from_db()
        self.assertGreaterEqual(state.upcoming_send_at, retry_at)
        self.assertFalse(self.process(state))


class APIAccountControlsTests(AccountControlsMixin, TestCase):
    provider = "api"

    def test_api_transport_rechecks_pipeline_after_message_was_queued(self):
        message = self.outbound()
        self._move_lead_to_other_pipeline()

        with patch(
            "services.channels.whatsapp_service.WhatsAppClient.send_text_message"
        ) as provider:
            with self.assertRaisesMessage(
                WhatsAppSendError,
                "current pipeline is linked to a different WhatsApp number",
            ):
                send_outbound_message(message=message)
        provider.assert_not_called()


class HostedAccountControlsTests(AccountControlsMixin, TestCase):
    provider = "hosted"

    def test_hosted_transport_rechecks_pipeline_after_message_was_queued(self):
        from services.channels.hosted_whatsapp_transport import send_hosted_message

        message = self.outbound()
        self._move_lead_to_other_pipeline()

        with patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message"
        ) as gateway:
            with self.assertRaisesMessage(
                WhatsAppSendError,
                "current pipeline is linked to a different WhatsApp number",
            ):
                send_hosted_message(message=message)
        gateway.assert_not_called()

    def test_hosted_ui_worker_blocks_stale_pipeline_sender(self):
        from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

        message = self.outbound()
        self._move_lead_to_other_pipeline()

        with patch(
            "apps.channels.hosted_send_tasks.WhatsAppWebClient.send_message"
        ) as gateway:
            result = send_hosted_whatsapp_message_task(str(message.pk))

        gateway.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertEqual(result["reason"], "pipeline_whatsapp_mismatch")

    def test_queued_followup_checks_switch_again_at_transport_boundary(self):
        from services.channels.hosted_automation_service import HostedAutomationPaused
        from services.channels.hosted_whatsapp_transport import send_hosted_message

        sequence, step, state = self.sequence()
        message = self.outbound()
        message.raw_payload = {"shvya_auto_followup": {"provider": "hosted", "sequence_id": str(sequence.pk)}}
        message.save(update_fields=["raw_payload", "updated_at"])
        FollowupExecution.objects.create(
            organization=self.org, state=state, lead=self.lead, sequence=sequence, step=step,
            scheduled_for=self.now, status=FollowupExecution.Status.PENDING, whatsapp_message=message,
        )
        self.save_settings(auto_follow_up=False)
        with patch("services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message") as gateway:
            with self.assertRaises(HostedAutomationPaused):
                send_hosted_message(message=message)
            gateway.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.QUEUED)

    def test_canonical_sender_does_not_strand_hosted_bump_messages(self):
        from apps.channels.tasks import send_whatsapp_message_task

        message = self.outbound({"origin": "bump_up", "number": 1})
        with patch(
            "services.channels.hosted_whatsapp_transport.WhatsAppWebClient.send_message",
            return_value={"messageId": "bump-sent"},
        ) as gateway, patch("services.channels.hosted_whatsapp_transport._push_chat_refresh"):
            send_whatsapp_message_task(str(message.pk))
        gateway.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.SENT)
