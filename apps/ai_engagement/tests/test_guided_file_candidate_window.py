from django.test import TestCase

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.file_sharing import FileSharingService
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.ai_engagement.tests.test_engagement_controls import AIEngagementControlTests
from apps.organizations.models import Organization


class GuidedFileCandidateWindowTests(TestCase):
    setUp = AIEngagementControlTests.setUp
    _inbound = AIEngagementControlTests._inbound

    def document(self, name, instruction, *, organization=None, **kwargs):
        return Document.objects.create(
            organization=organization or self.organization, name=name,
            share_instruction=instruction, file="guides/guide.pdf",
            processing_status="completed", **kwargs,
        )

    def candidates(self, text):
        source = self._inbound(f"candidate-{self.lead.whatsapp_messages.count()}")
        source.body = text
        source.save(update_fields=["body"])
        context = AIContextBuilder().build(organization=self.organization, lead=self.lead)
        return context, FileSharingService().build_file_candidates(organization=self.organization, context=context)

    def test_delivered_files_do_not_hide_remaining_guided_files(self):
        pending = self.document("Guide", "Share when useful to this lead.")
        sent = [self.document(f"Delivered {index}", "Share when useful to this lead.") for index in range(10)]
        self.lead.attributes = {STATE_KEY: {
            "shared_files": [{"document_id": item.pk, "status": "sent"} for item in sent],
            "private_marker": "internal runtime value",
        }}
        self.lead.save(update_fields=["attributes", "updated_at"])
        context, candidates = self.candidates("I want to learn more")
        self.assertEqual([item["document_id"] for item in candidates], [pending.pk])
        self.assertEqual(set(context.lead["shared_document_ids"]), {item.pk for item in sent})
        self.assertNotIn(STATE_KEY, context.lead["attributes"])

    def test_relevant_older_instruction_is_in_bounded_candidate_window(self):
        relevant = self.document("Onboarding guide", "Share when the lead needs enterprise onboarding.")
        for index in range(12):
            self.document(f"Unrelated {index}", "Share when a restaurant requests a catering quote.")
        _, candidates = self.candidates("We need enterprise onboarding")
        self.assertEqual(len(candidates), 10)
        self.assertEqual(candidates[0]["document_id"], relevant.pk)

    def test_explicit_request_allows_repeat_but_never_foreign_or_inactive_files(self):
        sent = self.document("Brochure", "Share on request.")
        foreign = Organization.objects.create(name="Other file tenant")
        self.document("Foreign brochure", "Share on request.", organization=foreign)
        self.document("Inactive brochure", "Share on request.", is_active=False)
        self.lead.attributes = {STATE_KEY: {"shared_files": [{"document_id": sent.pk, "status": "delivered"}]}}
        self.lead.save(update_fields=["attributes", "updated_at"])
        _, candidates = self.candidates("Please send the brochure again")
        self.assertEqual([item["document_id"] for item in candidates], [sent.pk])
        self.assertTrue(candidates[0]["already_shared"])
