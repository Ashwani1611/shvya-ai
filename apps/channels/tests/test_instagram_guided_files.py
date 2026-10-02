from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService
from apps.ai_engagement.services.instagram_files import queue_guided_file_reply
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.channels.instagram_models import InstagramConversation, InstagramMessage
from apps.channels.tests import test_instagram_ai as fixtures
from apps.organizations.models import Organization
from services.channels.instagram_ai import InstagramAIContextBuilder, execute_instagram_ai_engagement
from services.channels.instagram_service import send_queued_message


@override_settings(OPERATIONS_PUBLIC_ORIGIN="https://testserver")
class InstagramGuidedFileTests(TestCase):
    task = fixtures.InstagramAIEngagementTests.task

    def setUp(self):
        fixtures.InstagramAIEngagementTests.setUp(self)
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.document = Document.objects.create(
            organization=self.org, name="Product guide",
            file=SimpleUploadedFile("guide.txt", b"Product guide", content_type="text/plain"),
            share_instruction="Send when a lead asks for the product guide.",
            processing_status=Document.ProcessingStatus.COMPLETED, is_active=True,
        )

    def queue_file(self, **kwargs):
        with patch("services.channels.instagram_inbox.assert_reply_allowed"):
            return queue_guided_file_reply(
                organization=self.org, conversation=self.conversation,
                document_id=kwargs.get("document_id", self.document.pk),
                ai_metadata={"source_inbound_message_id": str(self.inbound.pk), "provider": "instagram"},
            )

    def mark_sent(self, message):
        message.status = InstagramMessage.Status.SENT
        message.sent_at = timezone.now()
        message.save(update_fields=["status", "sent_at"])

    def close_download(self, response):
        # A manually closed HttpResponse emits request_finished again, outside
        # the test client's guarded request lifecycle, closing PostgreSQL's
        # connection within TestCase's transaction. Exhausting its wrapped
        # iterator lets the client perform the guarded response cleanup itself.
        for _chunk in response.streaming_content:
            pass

    @patch("services.channels.instagram_ai._dispatch_instagram_ai_message")
    @patch("services.channels.instagram_inbox.assert_reply_allowed")
    @patch("services.channels.instagram_ai.EngagementService.engage")
    def test_selected_file_queues_link_and_reply_once_and_recovers_pending_file(self, engage, allowed, dispatch):
        engage.return_value = EngagementDecision(
            should_engage=True, message="Here is the product guide.",
            file_document_id=self.document.pk, crm_actions=[],
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="test",
        )
        with self.captureOnCommitCallbacks(execute=True):
            result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        rows = list(InstagramMessage.objects.filter(
            conversation=self.conversation, direction=InstagramMessage.Direction.OUTBOUND,
        ).order_by("created_at", "id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(result["message_ids"]), 2)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(rows[0].body, "Here is the product guide.")
        self.assertIn("https://testserver/", rows[1].body)
        self.assertEqual(rows[1].raw_payload["shvya_ai"]["file_document_id"], self.document.pk)
        self.mark_sent(rows[0])
        dispatch.reset_mock()
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(result["reason"], "existing_ai_response_requeued")
        dispatch.assert_called_once_with(rows[1].pk)
        self.assertEqual(self.conversation.messages.filter(direction="outbound").count(), 2)
        self.mark_sent(rows[1])
        dispatch.reset_mock()
        result = execute_instagram_ai_engagement(task=self.task(), message_id=self.inbound.pk)
        self.assertEqual(result["reason"], "duplicate_ai_response")
        dispatch.assert_not_called()

    def test_file_history_counts_only_confirmed_delivery_in_exact_conversation(self):
        message = self.queue_file()
        builder = InstagramAIContextBuilder(conversation_id=self.conversation.pk)
        self.assertNotIn(self.document.pk, builder._build_lead_context(lead=self.lead)["shared_document_ids"])
        self.mark_sent(message)
        self.assertIn(self.document.pk, builder._build_lead_context(lead=self.lead)["shared_document_ids"])
        self.assertEqual(builder._build_conversation_context(messages=[])["channel"], "instagram")

    def test_other_channel_or_thread_delivery_does_not_suppress_instagram_candidate(self):
        from types import SimpleNamespace

        self.lead.attributes = {STATE_KEY: {"shared_files": [
            {"document_id": self.document.pk, "status": "sent"},
        ]}}
        self.lead.save(update_fields=["attributes"])
        other_thread = InstagramConversation.objects.create(
            organization=self.org, account=self.account, lead=self.lead,
            participant_id="other-instagram-customer",
        )
        InstagramMessage.objects.create(
            organization=self.org, account=self.account, conversation=other_thread,
            direction=InstagramMessage.Direction.OUTBOUND, status=InstagramMessage.Status.SENT,
            sender_id=self.account.ig_user_id, recipient_id=other_thread.participant_id,
            body="Previously sent guide", raw_payload={"shvya_ai": {
                "file_document_id": self.document.pk,
            }},
        )
        builder = InstagramAIContextBuilder(conversation_id=self.conversation.pk)
        lead_context = builder._build_lead_context(lead=self.lead)
        self.assertEqual(lead_context["shared_document_ids"], [])
        context = SimpleNamespace(lead=lead_context, as_dict=lambda: {
            "lead": lead_context, "knowledge": [],
            "conversation": {"messages": [{"direction": "inbound", "body": "Tell me about your product"}]},
        })
        self.assertEqual(
            [item["document_id"] for item in FileSharingService().build_file_candidates(
                organization=self.org, context=context,
            )],
            [self.document.pk],
        )

    def test_signed_download_requires_delivery_and_is_revoked_when_file_is_retired(self):
        message = self.queue_file()
        path = urlsplit(message.body.splitlines()[-1]).path
        self.assertEqual(self.client.get(path).status_code, 404)
        self.mark_sent(message)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"Product guide")
        self.close_download(response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.document.is_active = False
        self.document.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_expired_or_modified_download_token_is_rejected(self):
        message = self.queue_file()
        self.mark_sent(message)
        path = urlsplit(message.body.splitlines()[-1]).path
        self.assertEqual(self.client.get(path.rstrip("/") + "x/").status_code, 404)
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 8 * 86400):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_cross_tenant_and_unguided_files_cannot_be_queued(self):
        foreign = Document.objects.create(
            organization=Organization.objects.create(name="Other files"),
            name="Foreign", file="knowledge/foreign.txt", share_instruction="Send",
            processing_status=Document.ProcessingStatus.COMPLETED, is_active=True,
        )
        with self.assertRaises(FileSharingError):
            self.queue_file(document_id=foreign.pk)
        self.document.share_instruction = ""
        self.document.save(update_fields=["share_instruction"])
        with self.assertRaises(FileSharingError):
            self.queue_file()
        self.assertFalse(self.conversation.messages.filter(direction="outbound").exists())

    @patch("services.channels.instagram_service._graph_post")
    def test_send_revalidates_file_before_provider_call(self, post):
        message = self.queue_file()
        self.document.share_instruction = ""
        self.document.save(update_fields=["share_instruction"])
        with self.assertRaises(FileSharingError):
            send_queued_message(message)
        post.assert_not_called()

    def test_file_version_change_revokes_existing_link(self):
        message = self.queue_file()
        self.mark_sent(message)
        path = urlsplit(message.body.splitlines()[-1]).path
        self.document.version += 1
        self.document.save(update_fields=["version"])
        self.assertEqual(self.client.get(path).status_code, 404)

    @patch("services.channels.instagram_service._graph_post", return_value={"message_id": "ig-ai-text-echo"})
    def test_provider_echo_preserves_ai_text_source_and_read_status(self, post):
        from services.channels.instagram_ai import _latest_customer_turn

        message = InstagramMessage.objects.create(
            organization=self.org, account=self.account, conversation=self.conversation,
            direction=InstagramMessage.Direction.OUTBOUND, status=InstagramMessage.Status.QUEUED,
            sender_id=self.account.ig_user_id, recipient_id=self.conversation.participant_id,
            body="Here is the information you requested.", raw_payload={"shvya_ai": {
                "source_inbound_message_id": str(self.inbound.pk), "provider": "instagram",
            }},
        )
        echo = InstagramMessage.objects.create(
            organization=self.org, account=self.account, conversation=self.conversation,
            direction=InstagramMessage.Direction.OUTBOUND, status=InstagramMessage.Status.READ,
            external_id="ig-ai-text-echo", body=message.body,
            sender_id=message.sender_id, recipient_id=message.recipient_id,
        )
        sent = send_queued_message(message)
        self.assertEqual(sent.pk, message.pk)
        self.assertEqual(sent.status, InstagramMessage.Status.READ)
        self.assertEqual(sent.raw_payload["shvya_ai"]["source_inbound_message_id"], str(self.inbound.pk))
        self.assertFalse(InstagramMessage.objects.filter(pk=echo.pk).exists())
        self.assertEqual(_latest_customer_turn(self.conversation).pk, self.inbound.pk)
        self.assertEqual(send_queued_message(sent).pk, message.pk)
        post.assert_called_once()

    @patch("services.channels.instagram_service._graph_post", return_value={"message_id": "ig-file-echo"})
    def test_provider_echo_preserves_signed_download_identity(self, post):
        message = self.queue_file()
        original_id = message.pk
        path = urlsplit(message.body.splitlines()[-1]).path
        InstagramMessage.objects.create(
            organization=self.org, account=message.account, conversation=self.conversation,
            direction=InstagramMessage.Direction.OUTBOUND, status=InstagramMessage.Status.SENT,
            external_id="ig-file-echo", body=message.body,
            sender_id=message.sender_id, recipient_id=message.recipient_id,
        )
        sent = send_queued_message(message)
        self.assertEqual(sent.pk, original_id)
        self.assertEqual(sent.external_id, "ig-file-echo")
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.close_download(response)

    @patch("services.channels.instagram_service._graph_post")
    def test_pdf_is_sent_as_native_attachment_with_short_lived_provider_grant(self, post):
        self.document.file.save("guide.pdf", SimpleUploadedFile("guide.pdf", b"%PDF-1.4\nexample"))
        message = self.queue_file()
        post.return_value = {"message_id": "native-pdf-message"}
        def provider_send(*args, **kwargs):
            payload = kwargs["payload"]["message"]
            self.assertEqual(payload["attachment"]["type"], "file")
            self.assertNotIn("text", payload)
            path = urlsplit(payload["attachment"]["payload"]["url"]).path
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.close_download(response)
            with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 16 * 60):
                self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.get(path.replace("provider-files/", "shared-files/")).status_code, 404)
            return {"message_id": "native-pdf-message"}
        post.side_effect = provider_send
        sent = send_queued_message(message)
        self.assertEqual(sent.status, InstagramMessage.Status.SENT)

    def test_provider_grant_cannot_serve_non_pdf_or_revoked_file(self):
        from apps.ai_engagement.services.instagram_files import shared_file_url
        message = self.queue_file()
        path = urlsplit(shared_file_url(message, provider_fetch=True)).path
        self.assertEqual(self.client.get(path).status_code, 404)
        self.document.file.save("guide.pdf", SimpleUploadedFile("guide.pdf", b"%PDF-1.4\nexample"))
        self.document.share_instruction = ""
        self.document.save(update_fields=["share_instruction"])
        self.assertEqual(self.client.get(path).status_code, 404)
