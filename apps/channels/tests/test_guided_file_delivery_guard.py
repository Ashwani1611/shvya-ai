"""AI file authorization is rechecked immediately before provider delivery."""
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.ai_engagement.models import Document
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import WhatsAppSendError, _send_outbound_media_message


class GuidedFileDeliveryGuardTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.organization = Organization.objects.create(name="Guided delivery")
        self.document = Document.objects.create(
            organization=self.organization, name="Product guide", is_active=True,
            file=SimpleUploadedFile("guide.txt", b"Product guide", content_type="text/plain"),
            share_instruction="Send when the lead asks for a guide.",
            processing_status=Document.ProcessingStatus.COMPLETED,
        )
        account = WhatsAppAccount.objects.create(
            organization=self.organization, phone_number_id="guided-delivery-account",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.message = WhatsAppMessage.objects.create(
            organization=self.organization, account=account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type=WhatsAppMessage.MessageType.DOCUMENT,
            status=WhatsAppMessage.Status.QUEUED, to_number="+919000000022",
            media_payload={"source": "document", "document_id": self.document.pk},
            raw_payload={"shvya_ai": {"source_inbound_message_id": "test-inbound"}},
        )
        self.provider = Mock()
        self.provider.upload_media.return_value = {"id": "uploaded-guide"}

    def test_removed_guidance_blocks_ai_send_before_upload(self):
        for instruction in ("", " \n "):
            with self.subTest(instruction=instruction):
                self.document.share_instruction = instruction
                self.document.save(update_fields=["share_instruction"])
                with self.assertRaises(WhatsAppSendError):
                    _send_outbound_media_message(client=self.provider, message=self.message)
        self.provider.upload_media.assert_not_called()
        self.provider.send_media_message.assert_not_called()

    def test_active_guidance_allows_ai_send(self):
        _send_outbound_media_message(client=self.provider, message=self.message)
        self.provider.upload_media.assert_called_once()
        self.assertEqual(
            self.provider.send_media_message.call_args.kwargs["media_id"],
            "uploaded-guide",
        )

    def test_api_and_coexistence_deliver_validated_file_despite_failed_index(self):
        self.document.is_active = False
        self.document.processing_status = "failed"
        self.document.file_sharing_ready = True
        self.document.save(update_fields=["is_active", "processing_status", "file_sharing_ready"])
        # API and Coexistence use this same Cloud API upload/send boundary.
        for connection_type in ("api", "coexistence"):
            with self.subTest(connection_type=connection_type):
                self.message.account.connection_type = connection_type
                self.provider.reset_mock()
                _send_outbound_media_message(client=self.provider, message=self.message)
                self.provider.upload_media.assert_called_once()
                self.provider.send_media_message.assert_called_once()

    def test_manual_document_send_does_not_require_ai_guidance(self):
        self.document.share_instruction = ""
        self.document.save(update_fields=["share_instruction"])
        self.message.raw_payload = {}
        _send_outbound_media_message(client=self.provider, message=self.message)
        self.provider.send_media_message.assert_called_once()
