"""Repeat file requests retain their candidates through the installed runtime."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.file_sharing import (
    FileSharingService, explicit_file_request, shared_document_ids,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.organizations.models import Organization


def file_context(text, *, shared=None, history=None):
    lead = {"attributes": {STATE_KEY: {"shared_files": history or []}}}
    if shared is not None:
        lead["shared_document_ids"] = shared
    return AIContext(
        organization={}, lead=lead, pipeline={}, stage={}, contacts=[], attributes=[],
        conversation={"messages": [{"id": "customer-turn", "direction": "inbound", "body": text}]},
        conversation_summary=None, qualification_notes=[], knowledge=[],
    )


class FileReshareRequestTests(SimpleTestCase):
    def candidates(self, text, *, shared=None, history=None):
        document = SimpleNamespace(
            id=7, name="Company Brochure", version=1, source_url="",
            share_instruction="Send when the customer requests company details or asks for the brochure again.",
        )
        with patch.object(FileSharingService, "get_eligible_documents", return_value=[document]):
            return FileSharingService().build_file_candidates(
                organization=SimpleNamespace(id="own-organization"),
                context=file_context(text, shared=shared, history=history),
            )

    def test_contextual_resend_keeps_previously_shared_candidate(self):
        for request in ("Please send it again", "Please resend it", "Share that once more",
                        "dobara bhej do", "phir se woh bhejo", "दोबारा भेज दीजिए", "फिर से वो भेजो"):
            with self.subTest(request=request):
                candidates = self.candidates(request, shared=[7])
                self.assertEqual([item["document_id"] for item in candidates], [7])
                self.assertTrue(candidates[0]["already_shared"])

    def test_hindi_file_nouns_keep_a_requested_repeat_available(self):
        for request in ("ब्रोशर भेज दीजिए", "फाइल फिर से भेजें", "कैटलॉग चाहिए", "पीडीएफ भेज दो"):
            with self.subTest(request=request):
                self.assertEqual([item["document_id"] for item in self.candidates(request, shared=[7])], [7])

    def test_greetings_and_unrelated_repetition_do_not_resend_files(self):
        for text in ("Hello", "Thanks", "Can you call again?", "Explain that again", "dobara call karo"):
            with self.subTest(text=text):
                self.assertEqual(self.candidates(text, shared=[7]), [])

    def test_implicit_resend_needs_real_shared_context(self):
        for text in ("Send it again", "Please resend", "dobara bhej do", "दोबारा भेजें"):
            with self.subTest(text=text):
                self.assertFalse(explicit_file_request(text))
                self.assertTrue(explicit_file_request(text, has_shared_files=True))

    def test_failed_and_queued_file_attempts_are_not_successful_history(self):
        history = [
            {"document_id": 7, "status": "failed"},
            {"document_id": 8, "status": "queued"},
            {"document_id": 9, "status": "sent"},
            {"document_id": 10, "status": "delivered"},
            {"document_id": 11, "status": "read"},
            {"document_id": True, "status": "sent"},
            {"document_id": "bad", "status": "sent"},
        ]
        self.assertEqual(shared_document_ids(file_context("Hello", history=history).lead), {9, 10, 11})
        candidates = self.candidates("Tell me about your company", history=[{"document_id": 7, "status": "failed"}])
        self.assertEqual([item["document_id"] for item in candidates], [7])
        self.assertFalse(candidates[0]["already_shared"])

    def test_provider_accepted_history_suppresses_only_unsolicited_repeats(self):
        history = [{"document_id": 7, "status": "sent"}]
        self.assertEqual(self.candidates("Hello", history=history), [])
        self.assertEqual([item["document_id"] for item in self.candidates("Send it again", history=history)], [7])

    def test_explicit_channel_history_does_not_inherit_another_channels_files(self):
        whatsapp_history = [{"document_id": 7, "status": "sent"}]
        # Instagram adapters supply this explicit empty list for a new DM.
        candidates = self.candidates("Hello", shared=[], history=whatsapp_history)
        self.assertEqual([item["document_id"] for item in candidates], [7])
        self.assertFalse(candidates[0]["already_shared"])
        self.assertEqual(self.candidates("Hello", shared=[7], history=whatsapp_history), [])
        self.assertFalse(explicit_file_request("Send it again", has_shared_files=bool(
            shared_document_ids(file_context("Hello", shared=[], history=whatsapp_history).lead)
        )))

    def test_first_enquiry_exposes_guided_file_without_an_explicit_file_noun(self):
        candidates = self.candidates("Hello")
        self.assertEqual([item["document_id"] for item in candidates], [7])
        self.assertEqual(candidates[0]["share_instruction"],
                         "Send when the customer requests company details or asks for the brochure again.")


class FileReshareTenantBoundaryTests(TestCase):
    def test_repeat_candidates_exclude_other_tenants_and_unready_files(self):
        organization = Organization.objects.create(name="Brochure Owner")
        other = Organization.objects.create(name="Other Brochure Owner")
        own = Document.objects.create(
            organization=organization, name="Company Brochure", file="knowledge/brochure.pdf",
            processing_status="completed", share_instruction="Send when the customer asks for our brochure.",
        )
        for owner, status, active in ((other, "completed", True), (organization, "pending", True),
                                      (organization, "failed", True), (organization, "completed", False)):
            Document.objects.create(
                organization=owner, name="Unavailable brochure", file="knowledge/unavailable.pdf",
                processing_status=status, is_active=active,
                share_instruction="Send when the customer asks for our brochure.",
            )
        candidates = FileSharingService().build_file_candidates(
            organization=organization, context=file_context("दोबारा भेज दीजिए", shared=[own.pk]),
        )
        self.assertEqual([item["document_id"] for item in candidates], [own.pk])

    def test_current_organization_timezone_remains_in_context(self):
        from apps.ai_engagement.services.context import AIContextBuilder

        organization = Organization.objects.create(name="London Business", timezone="Europe/London")
        context = AIContextBuilder()._build_organization_context(organization=organization)
        self.assertEqual(context["timezone"], "Europe/London")
