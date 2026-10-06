"""Grounding excerpts preserve the eligibility and exceptions around facts."""
from django.test import SimpleTestCase
from apps.ai_engagement.services.evidence_resolver import EvidenceResolver


class ScopedAboutEvidenceTests(SimpleTestCase):
    def test_conditional_price_keeps_parent_scope_and_period(self):
        source = ("# Offers\nOnly verified members are eligible.\n"
                  "## Women's special\nFor women only.\n\n"
                  "12-month membership: ₹1,000/month.\n\nNo personal training included.\n"
                  "# General plans\nContact the team for pricing.")
        text = EvidenceResolver._about_excerpt(source, question="What is the women's monthly price?",
                                              question_type="pricing")
        self.assertIn("Women's special", text)
        self.assertIn("For women only", text)
        self.assertIn("12-month", text)
        self.assertIn("Only verified members", text)
        self.assertIn("No personal training", text)

    def test_refund_fact_keeps_its_exception(self):
        source = "# Refund policy\nRefunds are available.\n\nExcept opened products.\n\nOnly within 14 days."
        text = EvidenceResolver._about_excerpt(source, question="Can opened products be refunded?",
                                              question_type="policy")
        self.assertIn("Except opened products", text)
        self.assertIn("Only within 14 days", text)

    def test_budget_never_cuts_a_fact_from_its_trailing_restriction(self):
        source = "# Conditional offer\n₹1,000/month.\n" + "Business detail. " * 400 + "\nFor women only."
        self.assertEqual(EvidenceResolver._about_excerpt(source, question="What is the price?",
                                                        question_type="pricing"), "")

    def test_weekday_rows_keep_timetable_and_day_headers(self):
        source = "# Weekly classes\nAdults only.\n## Friday\n09:00 – 09:50 BJJ Development\n## Saturday\n07:00 – 07:50 BJJ Performance"
        text = EvidenceResolver._about_excerpt(source, question="Friday BJJ times?", question_type="availability")
        self.assertIn("# Weekly classes", text)
        self.assertIn("Adults only", text)
        self.assertIn("## Friday", text)
        self.assertIn("09:00 – 09:50 BJJ Development", text)

    def test_plain_source_keeps_eligibility_paragraphs_together(self):
        source = "For members only.\n\nAnnual plan costs ₹5,000.\n\nNo cancellation refund."
        text = EvidenceResolver._about_excerpt(source, question="Annual plan price?", question_type="pricing")
        self.assertIn("For members only", text)
        self.assertIn("No cancellation refund", text)
