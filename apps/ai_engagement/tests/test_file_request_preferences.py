from django.test import SimpleTestCase

from apps.ai_engagement.services.file_sharing import declined_in_conversation, unrestricted_requested_document


class FileRequestPreferenceTests(SimpleTestCase):
    def setUp(self):
        self.candidate = {"document_id": 18, "name": "Product brochure", "already_shared": False,
                          "share_instruction": "Send this Product brochure with welcome message or when lead ask Product brochure."}
        self.history = [{"direction": "inbound", "body": "Do not send any files."}]

    def refused_after(self, text, candidate=None):
        return declined_in_conversation([*self.history, {"direction": "inbound", "body": text}],
                                        candidate or self.candidate)

    def test_direct_requests_with_language_and_format_preferences_reverse_refusal(self):
        for text in (
            "Hinglish mein brochure bhejo, please.",
            "Hindi mein reply karo brochure bhejo please.",
            "English mein brochure bhejiye.",
            "Please reply in English and send the uploaded brochure PDF as a file, rather than just a website link.",
            "Reply in Tamil and share the brochure.",
            "Please respond in English, and attach the uploaded brochure PDF as an attachment.",
            "Please send the attached brochure as a file instead of a link.",
            "Could you please send the product brochure?",
            "Please send the brochure.",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.refused_after(text))
                self.assertEqual(unrestricted_requested_document([self.candidate], text=text), 18)

    def test_content_mentions_quotes_and_conditional_requests_do_not_reverse_refusal(self):
        for text in (
            "Brochure mein kya hai?",
            "English mein brochure ke benefits batao.",
            "What does the brochure say?",
            "Please reply in English and explain the brochure contents.",
            "Don't send the brochure.",
            "Please do not send the uploaded brochure PDF.",
            "I said 'send the brochure' yesterday.",
            "The brochure says: send the brochure.",
            "Can you tell me whether you send the brochure?",
            "Please send the brochure when I reach Qualified.",
            "Please reply in English and send the invoice PDF.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.refused_after(text))
                self.assertIsNone(unrestricted_requested_document([self.candidate], text=text))

    def test_direct_request_does_not_override_sending_restrictions_or_choose_between_files(self):
        request = "Hinglish mein brochure bhejo, please."
        restricted = {**self.candidate, "share_instruction": self.candidate["share_instruction"] + " after qualification"}
        self.assertIsNone(unrestricted_requested_document([restricted], text=request))
        self.assertIsNone(unrestricted_requested_document(
            [self.candidate, {**self.candidate, "document_id": 19}], text=request))
        self.assertTrue(self.refused_after("Hinglish mein brochure bhejo, please. Do not send files."))

    def test_generic_file_request_reverses_refusal_without_selecting_a_named_document(self):
        request = "Send me the file now."
        self.assertFalse(self.refused_after(request))
        self.assertIsNone(unrestricted_requested_document([self.candidate], text=request))
