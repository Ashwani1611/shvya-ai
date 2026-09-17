"""Complete the isolated repair using realistic transport test fixtures."""

import ast
from pathlib import Path
import runpy


helpers = runpy.run_path(".repair/whatsapp_controls.py")
replace = helpers["replace"]

path = "services/channels/whatsapp_service.py"
replace(path, '\n    \n', '\n\n', count=2)
replace(path, '        # --------------------------------------------------------\n    # CREATE INBOUND MESSAGE', '    # --------------------------------------------------------\n    # CREATE INBOUND MESSAGE')

path = "services/channels/hosted_automation_service.py"
replace(path, '''        message = execution.whatsapp_message
        if message is None or message.status != WhatsAppMessage.Status.QUEUED:
''', '''        message = execution.whatsapp_message
        if message is not None and (
            message.account_id != account.pk or message.lead_id != state.lead_id
            or message.organization_id != state.organization_id
        ):
            if message.status == WhatsAppMessage.Status.QUEUED:
                message.status = WhatsAppMessage.Status.FAILED
                message.error = "Follow-up sender changed before delivery."
                message.save(update_fields=["status", "error", "updated_at"])
            message = None
        if message is None or message.status != WhatsAppMessage.Status.QUEUED:
''')

path = "apps/channels/tests/test_account_settings_controls.py"
replace(path, '    def outbound(self, ai=None):\n        message = queue_outbound_message(\n', '''    def outbound(self, ai=None):
        if ai is not None and not self.lead.whatsapp_messages.filter(direction="inbound").exists():
            self.inbound(self.lead.phone, "source-for-outbound")
        message = queue_outbound_message(
''')
replace(path, '''        self.save_settings(bump_up_count=1)
        with self.assertRaises(WhatsAppSendError):
            send_outbound_message(message=message)
''', '''        self.save_settings(bump_up_count=1)
        with self.assertRaisesMessage(WhatsAppSendError, "bump_up_limit_reached"):
            send_outbound_message(message=message)
''')
replace(path, '    def test_manual_message_is_not_disabled_by_automation_switches(self):\n', '''    def test_ai_message_sends_when_all_current_controls_allow_it(self):
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
''')
replace(path, '    def test_canonical_sender_does_not_strand_hosted_bump_messages(self):\n', '''    def test_queued_followup_checks_switch_again_at_transport_boundary(self):
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
''')

path = "apps/channels/tests/test_hosted_followup_media_inbox.py"
replace(path, '''        send_message.side_effect = WhatsAppWebGatewayError(
            "temporary gateway error",
''', '''        # A real scheduler-owned message has a scoped execution. Keep that
        # contract here so this test reaches the intended transient transport
        # failure instead of the final account-control safety gate.
        from apps.crm.models import Lead
        from apps.followups.models import FollowupExecution, FollowupSequence, FollowupStep
        from services.channels.hosted_whatsapp_service import update_session_settings
        from services.followup_service import assign_sequence

        update_session_settings(account=self.account, payload={
            "auto_follow_up": True, "business_hours_start": "00:00", "business_hours_end": "00:00",
        })
        lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.pipeline.stages.first(),
            name="Follow-up customer", phone="+919877776666",
        )
        sequence = FollowupSequence.objects.create(
            organization=self.org, name="Gateway retry", whatsapp_account=self.account, created_by=self.user,
        )
        step = FollowupStep.objects.create(
            sequence=sequence, position=1, step_type=FollowupStep.StepType.WHATSAPP,
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
        )
        state = assign_sequence(lead=lead, sequence=sequence, actor=self.user)
        execution = FollowupExecution.objects.create(
            organization=self.org, state=state, lead=lead, sequence=sequence, step=step,
            scheduled_for=state.upcoming_send_at, status=FollowupExecution.Status.PROCESSING,
        )
        send_message.side_effect = WhatsAppWebGatewayError(
            "temporary gateway error",
''')
replace(path, '''                    "sequence_id": "test-sequence",
''', '''                    "sequence_id": str(sequence.pk),
                    "execution_id": str(execution.pk),
''')
replace(path, '''        with self.assertRaises(HostedAutomationPaused):
            send_hosted_message(message=message, defer_on_pause=True)
''', '''        message.lead = lead
        message.save(update_fields=["lead", "updated_at"])
        execution.whatsapp_message = message
        execution.save(update_fields=["whatsapp_message", "updated_at"])

        with self.assertRaises(HostedAutomationPaused):
            send_hosted_message(message=message, defer_on_pause=True)
        send_message.assert_called_once()
''')

for changed in sorted(helpers["changed"]):
    ast.parse(Path(changed).read_text(), filename=changed)
