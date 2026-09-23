from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.models import SignupVerificationDelivery
from apps.accounts.tasks import deliver_signup_verifications
from django.utils import timezone


class SignupEmailVerificationTests(TestCase):
    def signup_post(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(*args, **kwargs)

    def signup_payload(self, email="new-owner@example.com"):
        return {
            "name": "New Owner",
            "email": email,
            "phone": "+919999999999",
            "company_name": "Pending Signup Org",
            "password": "StrongSignupPassword123!",
        }

    def test_signup_remains_inactive_until_email_verified(self):
        response = self.signup_post(
            reverse("crm-signup"),
            self.signup_payload(),
            REMOTE_ADDR="198.51.100.20",
        )

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="new-owner@example.com")
        organization = user.organization
        self.assertFalse(user.is_active)
        self.assertFalse(organization.is_active)
        self.assertEqual(len(mail.outbox), 1)

        body = mail.outbox[0].body
        verification_url = next(
            line for line in body.splitlines()
            if "/signup/verify-email/" in line
        )
        token = parse_qs(urlparse(verification_url).query)["token"][0]

        verified = self.client.get(
            reverse("crm-verify-email"),
            {"token": token},
            REMOTE_ADDR="198.51.100.20",
        )

        self.assertEqual(verified.status_code, 302)
        user.refresh_from_db()
        organization.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(organization.is_active)

    def test_tampered_verification_token_is_rejected(self):
        self.signup_post(
            reverse("crm-signup"),
            self.signup_payload(),
            REMOTE_ADDR="198.51.100.21",
        )
        verification_url = next(
            line for line in mail.outbox[0].body.splitlines()
            if "/signup/verify-email/" in line
        )
        token = parse_qs(urlparse(verification_url).query)["token"][0]

        response = self.client.get(
            reverse("crm-verify-email"),
            {"token": token + "tampered"},
            REMOTE_ADDR="198.51.100.21",
        )

        self.assertEqual(response.status_code, 400)
        user = User.objects.get(email="new-owner@example.com")
        self.assertFalse(user.is_active)
        self.assertFalse(user.organization.is_active)

    @patch(
        "apps.accounts.signup_delivery.send_mail",
        side_effect=RuntimeError("mail unavailable"),
    )
    def test_email_delivery_failure_preserves_account_and_retries(self, _send_mail):
        response = self.signup_post(
            reverse("crm-signup"), self.signup_payload(email="retry@example.com"),
            REMOTE_ADDR="198.51.100.22",
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="retry@example.com")
        self.assertFalse(user.is_active)
        self.assertFalse(user.organization.is_active)
        self.assertEqual(user.organization.pipelines.get(name="Leads").owner, user)
        delivery = SignupVerificationDelivery.objects.get(user=user)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.error_type, "RuntimeError")
        self.assertIsNone(delivery.delivered_at)
        self.assertGreater(delivery.next_attempt_at, timezone.now())
        SignupVerificationDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
        with patch("apps.accounts.signup_delivery.send_mail", return_value=1) as recovered:
            deliver_signup_verifications()
            deliver_signup_verifications()
        recovered.assert_called_once()
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivered_at)
        self.assertEqual(delivery.error_type, "")
        self.assertEqual(delivery.attempts, 2)

    @patch("apps.accounts.signup_delivery.send_mail", return_value=0)
    def test_zero_messages_sent_is_retried(self, _send_mail):
        response = self.signup_post(reverse("crm-signup"), self.signup_payload())
        self.assertEqual(response.status_code, 302)
        delivery = SignupVerificationDelivery.objects.get()
        self.assertIsNone(delivery.delivered_at)
        self.assertEqual(delivery.attempts, 1)

    @patch("apps.accounts.views_flat.SignupVerificationDelivery.objects.create", side_effect=RuntimeError("database failure"))
    def test_account_and_pipeline_roll_back_when_outbox_cannot_be_saved(self, _create):
        response = self.signup_post(reverse("crm-signup"), self.signup_payload())
        self.assertEqual(response.status_code, 500)
        self.assertFalse(User.objects.filter(email="new-owner@example.com").exists())
        from apps.organizations.models import Organization
        self.assertFalse(Organization.objects.filter(name="Pending Signup Org").exists())
