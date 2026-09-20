from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.crm.models import Pipeline, PipelinePermission
from apps.integrations.models import (
    EmailConfiguration,
    GoogleSheetIntegration,
    MetaLeadPage,
    WebhookConfiguration,
)
from apps.organizations.models import APIKey, Organization


class ConnectHubAuthorizationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Connect Hub Authorization Org"
        )
        self.admin = User.objects.create_user(
            email="connect-admin@example.com",
            password="test-password",
            name="Connect Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.agent = User.objects.create_user(
            email="connect-agent@example.com",
            password="test-password",
            name="Connect Agent",
            organization=self.organization,
            role=User.Role.AGENT,
        )
        self.pipeline = (
            Pipeline.objects.filter(
                organization=self.organization,
                is_active=True,
            ).first()
            or Pipeline.objects.create(
                organization=self.organization,
                name="Sales",
            )
        )

        # Deliberately grant the legacy pipeline-scoped API-key flag. An
        # organization-wide API key must still require organization-admin role.
        PipelinePermission.objects.create(
            pipeline=self.pipeline,
            user=self.agent,
            can_view_pipeline=True,
            can_manage_api_keys=True,
        )

    def authenticate(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = (
            session.session_key
        )

    def test_agent_cannot_create_or_revoke_organization_api_keys(self):
        self.authenticate(self.agent)

        create_response = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {"action": "create_key", "name": "Agent escalation key"},
        )
        self.assertEqual(create_response.status_code, 403)
        self.assertFalse(
            APIKey.objects.filter(
                organization=self.organization,
                name="Agent escalation key",
            ).exists()
        )

        existing, _ = APIKey.issue(
            organization=self.organization,
            name="Admin key",
        )
        revoke_response = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {"action": "revoke_key", "api_key_id": existing.pk},
        )
        self.assertEqual(revoke_response.status_code, 403)
        existing.refresh_from_db()
        self.assertTrue(existing.is_active)

    def test_agent_cannot_configure_organization_webhook(self):
        self.authenticate(self.agent)

        response = self.client.post(
            reverse("crm-connect-hub-webhook"),
            {
                "endpoint_url": "https://attacker.example/webhook",
                "secret": "agent-secret",
                "is_enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            WebhookConfiguration.objects.filter(
                organization=self.organization
            ).exists()
        )

    def test_agent_cannot_configure_organization_email(self):
        self.authenticate(self.agent)

        response = self.client.post(
            reverse("crm-connect-hub-email"),
            {
                "action": "save",
                "provider": "gmail",
                "email_address": "sales@example.com",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": "587",
                "smtp_security": "starttls",
                "smtp_username": "sales@example.com",
                "password": "agent-password",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            EmailConfiguration.objects.filter(
                organization=self.organization
            ).exists()
        )

    def test_agent_cannot_create_google_sheets_integration(self):
        self.authenticate(self.agent)

        response = self.client.post(
            reverse("crm-connect-hub-google-sheets"),
            {
                "action": "create",
                "name": "Agent Sheet",
                "pipeline_id": str(self.pipeline.pk),
                "stage_id": "",
                "spreadsheet_url": (
                    "https://docs.google.com/spreadsheets/d/"
                    "12345678901234567890/edit"
                ),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            GoogleSheetIntegration.objects.filter(
                organization=self.organization,
                name="Agent Sheet",
            ).exists()
        )

    def test_agent_cannot_change_meta_lead_ads_configuration(self):
        self.authenticate(self.agent)

        page_response = self.client.post(
            reverse("meta-lead-page-save"),
            {
                "page_id": "page-agent",
                "page_name": "Agent Page",
                "page_access_token": "agent-page-token",
                "app_secret": "agent-app-secret",
            },
        )
        self.assertEqual(page_response.status_code, 403)
        self.assertFalse(
            MetaLeadPage.objects.filter(
                organization=self.organization,
                page_id="page-agent",
            ).exists()
        )

        for route_name in (
            "meta-lead-page-delete",
            "meta-lead-form-save",
            "meta-lead-form-delete",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.post(reverse(route_name), {})
                self.assertEqual(response.status_code, 403)

    def test_agent_cannot_read_sensitive_connect_hub_management_pages(self):
        self.authenticate(self.agent)

        for route_name in (
            "crm-connect-hub-shvya-api",
            "crm-connect-hub-webhook",
            "crm-connect-hub-email",
            "crm-connect-hub-google-sheets",
            "crm-connect-hub-meta-lead-ad-forms",
            "crm-connect-hub-meta-conversions-api",
            "crm-connect-hub-razorpay",
            "crm-connect-hub-justdial",
            "crm-connect-hub-indiamart",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 403)

    def test_admin_can_open_sensitive_connect_hub_management_pages(self):
        self.authenticate(self.admin)

        for route_name in (
            "crm-connect-hub-shvya-api",
            "crm-connect-hub-webhook",
            "crm-connect-hub-email",
            "crm-connect-hub-google-sheets",
            "crm-connect-hub-meta-lead-ad-forms",
            "crm-connect-hub-meta-conversions-api",
            "crm-connect-hub-razorpay",
            "crm-connect-hub-justdial",
            "crm-connect-hub-indiamart",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
