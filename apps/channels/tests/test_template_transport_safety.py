"""Provider-boundary regressions without a database or customer messages."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from services.channels.whatsapp_service import WhatsAppSendError
from services.channels.whatsapp_template_delivery import TemplatePreparationError, _send_template_transport
from services.followup_service import FollowupDeliveryUnconfirmed, _handle_failure


class TemplateTransportSafetyTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(pk="account", organization_id="org", connection_type=WhatsAppAccount.ConnectionType.API,
                                       is_active=True, status="connected", phone_number_id="123", access_token="test-only")
        self.template = SimpleNamespace(pk="template", account_id="account", organization_id="org", name="hello", status="approved")
        self.lead = SimpleNamespace(pk="lead", organization_id="org", phone="+919000000001")
        self.message = SimpleNamespace(account=self.account, organization_id="org", lead_id="lead", lead=self.lead, to_number=self.lead.phone,
                                       media_payload={"template_id": "template", "template_name": "hello", "components": []},
                                       raw_payload={"shvya_auto_followup": {"step_id": "step"}}, save=Mock(), refresh_from_db=Mock(),
                                       status="queued", external_id=None)
        targets = {
            "services.channels.whatsapp_template_delivery.WhatsAppTemplate.objects.filter": self.template,
            "services.channels.whatsapp_template_delivery.base.WhatsAppClient": None,
            "apps.crm.models.Lead.objects.select_related": None,
            "services.followup_service.resolve_linked_whatsapp_account": self.account,
            "services.channels.template_media.resolve_delivery_media": [],
        }
        self.mocks = {}
        for target, result in targets.items():
            patcher = patch(target)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            self.mocks[target] = mocked
            mocked.return_value = result
        self.mocks["services.channels.whatsapp_template_delivery.WhatsAppTemplate.objects.filter"].return_value = Mock(first=Mock(return_value=self.template))
        self.mocks["apps.crm.models.Lead.objects.select_related"].return_value = Mock(filter=Mock(return_value=Mock(first=Mock(return_value=self.lead))))
        self.client = Mock()
        self.client.send_template_message.return_value = {"messages": [{"id": "wamid.success"}]}
        self.mocks["services.channels.whatsapp_template_delivery.base.WhatsAppClient"].return_value = self.client
        self.media = self.mocks["services.channels.template_media.resolve_delivery_media"]

    def test_success_records_provider_id_and_preserves_sequence_metadata(self):
        with patch("services.channels.ai_send_gate._reserve") as ai_reserve:
            _send_template_transport(self.message)
        ai_reserve.assert_not_called()
        self.message.refresh_from_db.assert_called_once()
        self.assertEqual(self.message.status, WhatsAppMessage.Status.SENT)
        self.assertEqual(self.message.external_id, "wamid.success")
        self.assertEqual(self.message.raw_payload["shvya_auto_followup"], {"step_id": "step"})
        self.client.send_template_message.assert_called_once_with(to=self.lead.phone, template_name="hello", language_code="en_US", components=[])

    def test_queued_welcome_cooldown_defers_before_media_upload(self):
        from django.utils import timezone
        from services.channels.ai_send_gate import AIMessageDeferred

        self.message.raw_payload = {"shvya_welcome": {"trigger": "lead_created"}}
        with (
            patch("services.channels.hosted_whatsapp_service.account_ai_block_reason", return_value=""),
            patch("services.channels.ai_send_gate._reserve", side_effect=AIMessageDeferred(timezone.now())),
        ):
            with self.assertRaises(AIMessageDeferred):
                _send_template_transport(self.message)
        self.assertEqual(self.message.status, "queued")
        self.media.assert_not_called()
        self.client.send_template_message.assert_not_called()

    def test_successful_welcome_preserves_metadata_for_shared_cooldown(self):
        welcome = {"trigger": "lead_created"}
        self.message.raw_payload = {"shvya_welcome": welcome}
        with (
            patch("services.channels.hosted_whatsapp_service.account_ai_block_reason", return_value=""),
            patch("services.channels.ai_send_gate._reserve", return_value="reservation"),
            patch("services.channels.ai_send_gate._admit_ai_provider"),
            patch("services.channels.ai_send_gate._finish") as finish,
        ):
            _send_template_transport(self.message)
        self.assertEqual(self.message.status, "sent")
        self.assertIsNotNone(self.message.sent_at)
        self.assertEqual(self.message.raw_payload["shvya_welcome"], welcome)
        finish.assert_called_once_with(self.message, "reservation")

    def test_pipeline_changed_after_queue_blocks_send(self):
        self.mocks["services.followup_service.resolve_linked_whatsapp_account"].return_value = SimpleNamespace(pk="other-account")
        with self.assertRaises(WhatsAppSendError) as caught:
            _send_template_transport(self.message)
        self.assertIsInstance(caught.exception.__cause__, TemplatePreparationError)
        self.assertEqual(self.message.status, WhatsAppMessage.Status.FAILED)
        self.media.assert_not_called()
        self.client.send_template_message.assert_not_called()

    def test_storage_upload_and_cache_failures_are_known_not_sent(self):
        for error in (OSError("missing file"), ConnectionError("cache unavailable"), WhatsAppAPIError("media upload timeout")):
            with self.subTest(error=type(error).__name__):
                self.media.side_effect = error
                with self.assertRaises(WhatsAppSendError) as caught:
                    _send_template_transport(self.message)
                self.assertIsInstance(caught.exception.__cause__, TemplatePreparationError)
                self.assertEqual(self.message.status, WhatsAppMessage.Status.FAILED)
                self.client.send_template_message.assert_not_called()

    def test_no_provider_message_id_is_never_marked_sent(self):
        for response in ({}, {"messages": []}, {"messages": [{}]}, {"messages": "malformed"}):
            with self.subTest(response=response):
                self.client.send_template_message.return_value = response
                with self.assertRaises(WhatsAppSendError) as caught:
                    _send_template_transport(self.message)
                self.assertIsInstance(caught.exception.__cause__, WhatsAppAPIError)
                self.assertIsNone(caught.exception.__cause__.status_code)
                self.assertEqual(self.message.status, WhatsAppMessage.Status.FAILED)
                self.assertIsNone(self.message.external_id)

    def test_meta_400_preserves_exact_provider_error(self):
        self.client.send_template_message.side_effect = WhatsAppAPIError("Meta returned 400", status_code=400,
            response_body='{"error":{"code":132000,"message":"Parameter mismatch"}}')
        with self.assertRaises(WhatsAppSendError):
            _send_template_transport(self.message)
        self.assertEqual(self.message.raw_payload["meta_error_response"]["error"]["code"], 132000)
        self.assertIn("132000", self.message.error)

    def test_sequence_uncertain_delivery_pauses_without_automatic_retry(self):
        state = SimpleNamespace(save=Mock())
        execution = SimpleNamespace(attempt_no=1, max_attempts=3, save=Mock())
        _handle_failure(state, execution, FollowupDeliveryUnconfirmed("Review original delivery"))
        self.assertEqual(state.status, "paused")
        self.assertIsNone(state.upcoming_send_at)
        self.assertEqual(execution.status, "failed")

    def test_sequence_configuration_error_pauses_without_retry(self):
        state = SimpleNamespace(save=Mock())
        execution = SimpleNamespace(attempt_no=1, max_attempts=3, save=Mock())
        _handle_failure(state, execution, TemplatePreparationError("Attach media first"))
        self.assertEqual(state.status, "paused")
        self.assertIsNone(state.upcoming_send_at)
        self.assertEqual(execution.status, "failed")

    def test_disconnected_credentials_are_blocked_before_provider(self):
        self.account.access_token = ""
        with self.assertRaises(WhatsAppSendError) as caught:
            _send_template_transport(self.message)
        self.assertIsInstance(caught.exception.__cause__, TemplatePreparationError)
        self.client.send_template_message.assert_not_called()

    def test_media_components_reach_api_for_image_video_and_document(self):
        for kind in ("image", "video", "document"):
            with self.subTest(kind=kind):
                components = [{"type": "header", "parameters": [{"type": kind, kind: {"id": "123456"}}]}]
                self.media.return_value = components
                self.client.reset_mock()
                _send_template_transport(self.message)
                self.assertEqual(self.client.send_template_message.call_args.kwargs["components"], components)
                self.assertEqual(self.message.status, "sent")


class SequenceTemplateTransportTests(SimpleTestCase):
    def setUp(self):
        from django.utils import timezone
        self.now = timezone.now()
        account = SimpleNamespace(id="account", status="connected", is_active=True, phone_number_id="123")
        self.template = SimpleNamespace(id="template", pk="template", name="offer", status="approved", account_id="account")
        self.state = SimpleNamespace(save=Mock(), lead=SimpleNamespace(phone="+919000000001"), organization="org", sequence_id="sequence",
                                     sequence=SimpleNamespace(whatsapp_account=account, created_by=None))
        self.step = SimpleNamespace(id="step", whatsapp_template=self.template)
        self.execution = SimpleNamespace(attempt_no=1, save=Mock())
        self.sender = SimpleNamespace(next_available_at=None, save=Mock())
        self.components = [{"type": "header", "parameters": [{"type": "image", "image": {"shvya_asset": "asset"}}]}]
        self.message = SimpleNamespace(external_id="wamid.sequence")
        patches = {
            "FollowupSenderState.objects.select_for_update": Mock(get_or_create=Mock(return_value=(self.sender, False))),
            "render_for_lead": {"body_text": "Hi Asha", "components": self.components},
            "WhatsAppTemplateMetadata.objects.filter": Mock(first=Mock(return_value=SimpleNamespace(language="en_US"))),
            "live_followup_due": self.now,
            "WhatsAppMessage.objects.create": self.message,
            "whatsapp_service.send_outbound_message": self.message,
            "_repeat_or_advance": None,
        }
        self.mocks = {}
        for name, value in patches.items():
            patcher = patch(f"services.followup_service.{name}", return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_sequence_passes_media_and_text_through_canonical_template_transport(self):
        from services.followup_service import _send_whatsapp_step
        _send_whatsapp_step(self.state, self.step, self.execution)
        create = self.mocks["WhatsAppMessage.objects.create"].call_args.kwargs
        self.assertEqual(create["body"], "Hi Asha")
        self.assertEqual(create["media_payload"]["components"], self.components)
        self.assertEqual(create["media_payload"]["transport"], "template")
        self.mocks["whatsapp_service.send_outbound_message"].assert_called_once_with(message=self.message)
        self.assertEqual(self.execution.payload["meta_message_id"], "wamid.sequence")
        self.assertEqual(self.execution.status, "sent")

    def test_sequence_does_not_advance_after_unconfirmed_send(self):
        from services.followup_service import _send_whatsapp_step
        failure = WhatsAppSendError("Unconfirmed")
        failure.__cause__ = WhatsAppAPIError("Request timed out")
        self.mocks["whatsapp_service.send_outbound_message"].side_effect = failure
        with self.assertRaises(FollowupDeliveryUnconfirmed):
            _send_whatsapp_step(self.state, self.step, self.execution)
        self.mocks["_repeat_or_advance"].assert_not_called()
        self.sender.save.assert_not_called()
