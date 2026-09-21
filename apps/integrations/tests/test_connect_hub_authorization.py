from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import (
    get_session_cookie_name,
    set_authenticated_user,
)
from apps.crm.models import Pipeline, PipelinePermission
from apps.integrations.models import (
    DiagnosticOAuthAuthorizationCode,
    DiagnosticOAuthClient,
    DiagnosticOAuthToken,
    EmailConfiguration,
    GoogleSheetIntegration,
    MetaLeadPage,
    OperationsAuditEvent,
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsPolicy,
    WebhookConfiguration,
)
from apps.integrations.operations_auth import token_hash
from apps.integrations.operations_policy import (
    CAP_AUDIT_READ,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
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

    def test_admin_revoke_diagnostic_key_revokes_bound_oauth_grants(self):
        self.authenticate(self.admin)
        api_key, _ = APIKey.issue(
            organization=self.organization,
            name="Diagnostic key",
        )
        api_key.can_upsert_leads = False
        api_key.can_read_diagnostics = True
        api_key.save(
            update_fields=["can_upsert_leads", "can_read_diagnostics"]
        )
        client = DiagnosticOAuthClient.objects.create(
            client_id="diagnostic-revoke-client",
            client_name="External AI",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        token = DiagnosticOAuthToken.objects.create(
            client=client,
            api_key=api_key,
            organization=self.organization,
            access_token_hash="a" * 64,
            refresh_token_hash="b" * 64,
            scope="diagnostics.read offline_access",
            resource="https://example.test/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=30),
        )
        code = DiagnosticOAuthAuthorizationCode.objects.create(
            client=client,
            api_key=api_key,
            organization=self.organization,
            code_hash="c" * 64,
            redirect_uri="https://chatgpt.com/aip/callback",
            scope="diagnostics.read",
            code_challenge="d" * 43,
            resource="https://example.test/mcp/",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        response = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {"action": "revoke_key", "api_key_id": api_key.pk},
        )
        self.assertEqual(response.status_code, 302)
        api_key.refresh_from_db()
        token.refresh_from_db()
        code.refresh_from_db()
        self.assertFalse(api_key.is_active)
        self.assertIsNotNone(token.revoked_at)
        self.assertLessEqual(code.expires_at, timezone.now())

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

    def test_admin_can_review_and_revoke_tenant_operations_sessions_without_tokens_leaking(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        client = OperationsOAuthClient.objects.create(
            client_id="connect-hub-session-client",
            client_name="ChatGPT Operations",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        raw_access = "panel-access-token-secret"
        raw_refresh = "panel-refresh-token-secret"
        token = OperationsOAuthToken.objects.create(
            client=client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash(raw_access),
            refresh_token_hash=token_hash(raw_refresh),
            scope="operations.read operations.write",
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )

        other_org = Organization.objects.create(name="Foreign Session Org")
        other_admin = User.objects.create_user(
            email="foreign-session-admin@example.com",
            password="test-password",
            name="Foreign Session Admin",
            organization=other_org,
            role=User.Role.ADMIN,
        )
        other_client = OperationsOAuthClient.objects.create(
            client_id="foreign-session-client",
            client_name="Foreign Claude Operations",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        foreign_token = OperationsOAuthToken.objects.create(
            client=other_client,
            actor=other_admin,
            organization=other_org,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash("foreign-access-token"),
            refresh_token_hash=token_hash("foreign-refresh-token"),
            scope="operations.read",
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )

        self.authenticate(self.admin)
        response = self.client.get(
            reverse("crm-connect-hub-shvya-api")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Connected External AI Sessions", body)
        self.assertIn("ChatGPT Operations", body)
        self.assertIn(self.admin.name, body)
        self.assertNotIn("Foreign Claude Operations", body)
        self.assertNotIn(raw_access, body)
        self.assertNotIn(raw_refresh, body)
        self.assertNotIn(token.access_token_hash, body)
        self.assertNotIn(token.refresh_token_hash, body)

        revoke = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {
                "action": "revoke_operations_session",
                "operations_token_id": str(token.id),
            },
        )
        self.assertEqual(revoke.status_code, 302)
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

        audit = OperationsAuditEvent.objects.get(
            organization=self.organization,
            tool_name="oauth_revoke_dashboard",
            target_id=str(token.id),
        )
        self.assertEqual(audit.actor_id, self.admin.id)
        self.assertEqual(audit.outcome, OperationsAuditEvent.Outcome.SUCCESS)
        self.assertNotIn(raw_access, str(audit.change_summary))
        self.assertNotIn(raw_refresh, str(audit.change_summary))

        foreign_revoke = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {
                "action": "revoke_operations_session",
                "operations_token_id": str(foreign_token.id),
            },
        )
        self.assertEqual(foreign_revoke.status_code, 404)
        foreign_token.refresh_from_db()
        self.assertIsNone(foreign_token.revoked_at)

    def test_agent_cannot_revoke_operations_oauth_session(self):
        client = OperationsOAuthClient.objects.create(
            client_id="agent-revoke-client",
            client_name="Agent Revoke Test",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        token = OperationsOAuthToken.objects.create(
            client=client,
            actor=self.admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash("agent-revoke-access"),
            refresh_token_hash=token_hash("agent-revoke-refresh"),
            scope="operations.read",
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        self.authenticate(self.agent)

        response = self.client.post(
            reverse("crm-connect-hub-shvya-api"),
            {
                "action": "revoke_operations_session",
                "operations_token_id": str(token.id),
            },
        )
        self.assertEqual(response.status_code, 403)
        token.refresh_from_db()
        self.assertIsNone(token.revoked_at)

    def test_admin_operations_panel_marks_stale_human_authority_without_token_leak(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        former_admin = User.objects.create_user(
            email="former-operations-admin@example.com",
            password="test-password",
            name="Former Operations Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        client = OperationsOAuthClient.objects.create(
            client_id="stale-authority-client",
            client_name="Claude Operations",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        raw_access = "stale-authority-access-secret"
        raw_refresh = "stale-authority-refresh-secret"
        OperationsOAuthToken.objects.create(
            client=client,
            actor=former_admin,
            organization=self.organization,
            role=ROLE_ORGANIZATION_ADMIN,
            access_token_hash=token_hash(raw_access),
            refresh_token_hash=token_hash(raw_refresh),
            scope="operations.read",
            granted_capabilities=[CAP_ORGANIZATION_READ],
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=1),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )

        former_admin.role = User.Role.AGENT
        former_admin.save(update_fields=["role", "updated_at"])

        self.authenticate(self.admin)
        response = self.client.get(
            reverse("crm-connect-hub-shvya-api")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Claude Operations", body)
        self.assertIn("Former Operations Admin", body)
        self.assertIn("Reconnect required", body)
        self.assertIn(
            "no longer matches the current SHVYA user/tenant authority",
            body,
        )
        self.assertNotIn(raw_access, body)
        self.assertNotIn(raw_refresh, body)

    def test_admin_operations_panel_shows_only_tenant_audit_when_enabled(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUDIT_READ,
            ],
            approval_required_capabilities=[],
        )
        own = OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="get_organization_configuration",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Review organization setup",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="a" * 64,
            change_summary={"result": "safe"},
        )
        other_org = Organization.objects.create(name="Other Audit Org")
        other_admin = User.objects.create_user(
            email="other-audit-admin@example.com",
            password="test-password",
            name="Other Audit Admin",
            organization=other_org,
            role=User.Role.ADMIN,
        )
        other = OperationsAuditEvent.objects.create(
            actor=other_admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=other_org,
            tool_name="foreign_tenant_tool",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(other_org.id),
            reason="Foreign tenant activity",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="b" * 64,
            change_summary={"result": "foreign"},
        )

        self.authenticate(self.admin)
        response = self.client.get(
            reverse("crm-connect-hub-shvya-api")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Recent Operations Activity", body)
        self.assertIn(own.tool_name, body)
        self.assertIn(str(own.id), body)
        self.assertIn("Review organization setup", body)
        self.assertNotIn(other.tool_name, body)
        self.assertNotIn(str(other.id), body)
        self.assertNotIn("Foreign tenant activity", body)

    def test_admin_can_review_audit_after_external_ai_policy_is_disabled(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=False,
            allowed_capabilities=[
                CAP_ORGANIZATION_READ,
                CAP_AUDIT_READ,
            ],
            approval_required_capabilities=[],
        )
        event = OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="historical_operations_event",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Reviewable after external AI is disabled",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="h" * 64,
            change_summary={"status": "safe"},
        )

        self.authenticate(self.admin)
        response = self.client.get(
            reverse("crm-connect-hub-shvya-api")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Disabled by SHVYA Superadmin", body)
        self.assertIn("Recent Operations Activity", body)
        self.assertIn(str(event.id), body)
        self.assertIn(
            "Reviewable after external AI is disabled",
            body,
        )

    def test_admin_operations_panel_hides_audit_when_capability_not_granted(self):
        OperationsPolicy.objects.create(
            organization=self.organization,
            organization_admin_enabled=True,
            allowed_capabilities=[CAP_ORGANIZATION_READ],
            approval_required_capabilities=[],
        )
        OperationsAuditEvent.objects.create(
            actor=self.admin,
            role=ROLE_ORGANIZATION_ADMIN,
            organization=self.organization,
            tool_name="hidden_audit_tool",
            capability=CAP_ORGANIZATION_READ,
            target_type="organization",
            target_id=str(self.organization.id),
            reason="Should stay hidden in panel",
            outcome=OperationsAuditEvent.Outcome.SUCCESS,
            request_fingerprint="c" * 64,
            change_summary={},
        )

        self.authenticate(self.admin)
        response = self.client.get(
            reverse("crm-connect-hub-shvya-api")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("Recent Operations Activity", body)
        self.assertNotIn("hidden_audit_tool", body)
        self.assertNotIn("Should stay hidden in panel", body)

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
