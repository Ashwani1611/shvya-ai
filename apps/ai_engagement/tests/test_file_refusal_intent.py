"""Attachment refusals do not become resend permission."""
from unittest import TestCase

from apps.ai_engagement.services.file_sharing import declined_file_request, declined_in_conversation, reconcile_welcome_document


class FileRefusalIntentTests(TestCase):
    candidate = {"document_id": 18, "name": "product brochure", "already_shared": True}

    def test_negative_resend_is_a_refusal(self):
        for body in ("Do not resend the file. Just explain its contents.", "Don't re-send the product brochure.", "Never reshare the brochure.", "No need to reattach the PDF."):
            with self.subTest(body=body):
                self.assertTrue(declined_file_request(body, self.candidate))

    def test_short_noun_refusals_are_respected(self):
        for body in ("No file please.", "Please no more attachments.", "No product brochure thanks.", "No PDF."):
            with self.subTest(body=body):
                self.assertTrue(declined_file_request(body, self.candidate))

    def test_short_document_label_refusal_matches_that_candidate(self):
        self.assertTrue(declined_file_request("Do not send the brochure.", self.candidate))
        self.assertTrue(declined_file_request("brochure mat bhejna", self.candidate))

    def test_refusal_does_not_cancel_a_different_named_document(self):
        candidate = {"name": "pricing sheet"}
        self.assertFalse(declined_file_request("Do not resend the product brochure.", candidate))

    def test_absence_facts_and_content_questions_are_not_refusals(self):
        for body in ("No file was uploaded.", "The brochure has no refund terms.", "Does the file describe pricing?", "Please resend the brochure."):
            with self.subTest(body=body):
                self.assertFalse(declined_file_request(body, self.candidate))

    def test_content_question_does_not_reverse_the_prior_refusal(self):
        messages = [{"direction": "inbound", "body": "Do not resend the file."}, {"direction": "inbound", "body": "What does the brochure say about billing?"}]
        self.assertTrue(declined_in_conversation(messages, self.candidate))

    def test_later_direct_request_can_reverse_the_refusal(self):
        messages = [{"direction": "inbound", "body": "No file please."}, {"direction": "inbound", "body": "Please resend the product brochure."}]
        self.assertFalse(declined_in_conversation(messages, self.candidate))

    def test_assistant_messages_do_not_reverse_customer_refusal(self):
        messages = [{"direction": "inbound", "body": "No file please."}, {"direction": "outbound", "body": "Please resend the product brochure."}]
        self.assertTrue(declined_in_conversation(messages, self.candidate))

    def test_content_question_cannot_authorize_an_exact_welcome_request_rule(self):
        candidate = {**self.candidate, "share_instruction": "send this product brochure along with welcome message or when ever lead ask product brochure."}
        for body in ("What does the brochure say about billing?", "Can I upload a PDF as a knowledge source?", "Do not resend the file. Just explain its contents."):
            with self.subTest(body=body):
                self.assertIsNone(reconcile_welcome_document(18, [candidate], welcome_due=False, explicit_request=True, requested_text=body))
        self.assertEqual(reconcile_welcome_document(18, [candidate], welcome_due=False, explicit_request=True, requested_text="Please send the product brochure again."), 18)
        self.assertEqual(reconcile_welcome_document(18, [candidate], welcome_due=True, explicit_request=False, requested_text="Hello"), 18)

    def test_complex_authored_file_condition_is_not_rewritten_by_exact_rule_guard(self):
        candidate = {**self.candidate, "share_instruction": "Send only to qualified Instagram leads who request a brochure."}
        self.assertEqual(reconcile_welcome_document(18, [candidate], welcome_due=False, explicit_request=False, requested_text="Tell me more."), 18)
