from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger


class SmartTriggerSignalTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Signal Trigger Org")
        self.user = User.objects.create_user(
            email="signal-trigger@example.com",
            organization=self.organization,
            password="test-password",
            name="Signal Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Leads",
        )
        self.stages = list(
            Stage.objects.filter(pipeline=self.pipeline).order_by("display_order")[:2]
        )

    def _listener(self, event_type, name):
        return SmartTrigger.objects.create(
            organization=self.organization,
            created_by=self.user,
            name=name,
            event_type=event_type,
            actions=[{"type": "clear_sequence"}],
        )

    @patch("apps.triggers.signals.queue_trigger_event")
    def test_lead_created_event_is_published_after_commit(self, queue_mock):
        self._listener(SmartTrigger.EventType.LEAD_CREATED, "Lead created listener")

        with self.captureOnCommitCallbacks(execute=True):
            lead = Lead.objects.create(
                organization=self.organization,
                pipeline=self.pipeline,
                stage=self.stages[0],
                name="Signal Lead",
                phone="+919333333333",
            )

        queue_mock.assert_called_once()
        kwargs = queue_mock.call_args.kwargs
        self.assertEqual(kwargs["organization_id"], self.organization.id)
        self.assertEqual(kwargs["lead_id"], lead.id)
        self.assertEqual(kwargs["event_type"], SmartTrigger.EventType.LEAD_CREATED)

    @patch("apps.triggers.signals.queue_trigger_event")
    def test_stage_change_publishes_stage_and_updated_events(self, queue_mock):
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stages[0],
            name="Stage Lead",
            phone="+919444444444",
        )
        self._listener(
            SmartTrigger.EventType.LEAD_STAGE_CHANGED,
            "Stage listener",
        )
        self._listener(SmartTrigger.EventType.LEAD_UPDATED, "Updated listener")

        with self.captureOnCommitCallbacks(execute=True):
            lead.stage = self.stages[1]
            lead.save(update_fields=["stage", "updated_at"])

        event_types = [call.kwargs["event_type"] for call in queue_mock.call_args_list]
        self.assertEqual(
            event_types,
            [
                SmartTrigger.EventType.LEAD_STAGE_CHANGED,
                SmartTrigger.EventType.LEAD_UPDATED,
            ],
        )
        self.assertIn("stage_id", queue_mock.call_args_list[0].kwargs["payload"]["changed_fields"])

    @patch("apps.triggers.signals.queue_trigger_event")
    def test_inbound_whatsapp_event_includes_message_body(self, queue_mock):
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stages[0],
            name="WhatsApp Lead",
            phone="+919555555555",
        )
        self._listener(
            SmartTrigger.EventType.WHATSAPP_RECEIVED,
            "WhatsApp listener",
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Signal Sender",
            phone_number_id="signal-phone-id",
            display_phone_number="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            message = WhatsAppMessage.objects.create(
                organization=self.organization,
                account=account,
                lead=lead,
                direction=WhatsAppMessage.Direction.INBOUND,
                external_id="wamid.signal-test",
                from_number="919555555555",
                to_number="919999999999",
                body="I am interested",
                status=WhatsAppMessage.Status.RECEIVED,
            )

        queue_mock.assert_called_once()
        kwargs = queue_mock.call_args.kwargs
        self.assertEqual(kwargs["lead_id"], lead.id)
        self.assertEqual(kwargs["event_type"], SmartTrigger.EventType.WHATSAPP_RECEIVED)
        self.assertEqual(kwargs["payload"]["message_id"], str(message.id))
        self.assertEqual(kwargs["payload"]["message_body"], "I am interested")
