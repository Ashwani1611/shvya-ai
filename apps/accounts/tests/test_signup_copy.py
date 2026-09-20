"""Public signup copy and legal-link regressions."""
from django.test import TestCase
from django.urls import reverse


class SignupCopyTests(TestCase):
    def test_signup_shows_consent_links_and_trial_cta(self):
        response = self.client.get(reverse("crm-signup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "By signing up, you agree to SHVYA")
        self.assertContains(
            response,
            'href="/terms-conditions/" target="_blank" rel="noopener noreferrer"',
        )
        self.assertContains(
            response,
            'href="/privacy-policy/" target="_blank" rel="noopener noreferrer"',
        )
        self.assertContains(response, "service-related and marketing communications")
        self.assertContains(response, "Start My Trial")
        self.assertNotContains(response, "<span>Create account</span>")
