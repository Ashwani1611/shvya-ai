from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.organizations.models import Organization


class SignupEmailVerificationTests(TestCase):
    def signup_payload(self, email="new-owner@example.com"):
        return {
            "name": "New Owner",
            "email": email,
            "phone": "+919999999999",
            "company_name": "Pending Signup Org",
            "password": "StrongSignupPassword123!",
        }

    def test_signup_remains_inactive_until_email_verified(self):
        response = self.client.post(
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
        self.client.post(
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
        "apps.accounts.views_flat.send_mail",
        side_effect=RuntimeError("mail unavailable"),
    )
    def test_email_delivery_failure_rolls_back_pending_account(self, _send_mail):
        response = self.client.post(
            reverse("crm-signup"),
            self.signup_payload(email="rollback@example.com"),
            REMOTE_ADDR="198.51.100.22",
        )

        self.assertEqual(response.status_code, 500)
        self.assertFalse(
            User.objects.filter(email="rollback@example.com").exists()
        )
        self.assertFalse(
            Organization.objects.filter(name="Pending Signup Org").exists()
        )
