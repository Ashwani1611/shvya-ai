from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.integrations.operations_auth import token_hash
from apps.integrations.operations_models import (
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import ROLE_SUPERADMIN
from apps.organizations.models import Organization


class SuperadminGlobalMCPWorkspaceTests(TestCase):
    def setUp(self):
        self.password = "StrongSuperadminPassword123!"
        self.superadmin = User.objects.create_superuser(
            email="mcp-superadmin@example.com",
            password=self.password,
            name="MCP Superadmin",
        )
        self.organization = Organization.objects.create(
            name="Global MCP Client",
        )
        login = self.client.post(
            reverse("superadmin-login"),
            {
                "username": self.superadmin.email,
                "password": self.password,
            },
        )
        self.assertEqual(login.status_code, 302)

    def _superadmin_grant(self):
        client = OperationsOAuthClient.objects.create(
            client_id="global_mcp_test_client",
            client_name="ChatGPT MCP",
            redirect_uris=["https://chatgpt.com/aip/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        token = OperationsOAuthToken.objects.create(
            client=client,
            actor=self.superadmin,
            organization=None,
            active_organization=self.organization,
            role=ROLE_SUPERADMIN,
            access_token_hash=token_hash("global-mcp-access"),
            refresh_token_hash=token_hash("global-mcp-refresh"),
            scope="operations.read operations.write offline_access",
            granted_capabilities=[],
            resource="http://testserver/operations/mcp/",
            expires_at=timezone.now() + timedelta(hours=4),
            refresh_expires_at=timezone.now() + timedelta(days=14),
        )
        support = OperationsSupportSession.objects.create(
            token=token,
            actor=self.superadmin,
            organization=self.organization,
            reason="Investigate organization automation configuration",
        )
        return token, support

    def test_global_mcp_workspace_is_available_outside_organization_page(self):
        token, support = self._superadmin_grant()

        response = self.client.get(
            reverse("superadmin-operations-mcp")
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Superadmin MCP", body)
        self.assertIn("External AI control plane", body)
        self.assertIn(
            "http://testserver/operations/mcp/",
            body,
        )
        self.assertIn(
            "This endpoint is global",
            body,
        )
        self.assertIn("ChatGPT MCP", body)
        self.assertIn(self.organization.name, body)
        self.assertIn(str(token.id), body)
        self.assertNotIn(
            "global-mcp-access",
            body,
        )
        self.assertNotIn(
            "global-mcp-refresh",
            body,
        )
        self.assertIsNone(support.ended_at)

    def test_sidebar_has_dedicated_global_mcp_navigation(self):
        response = self.client.get(
            reverse("superadmin-org-list")
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(
            reverse("superadmin-operations-mcp"),
            body,
        )
        self.assertIn(">MCP</span>", body)

    def test_organization_page_no_longer_hosts_superadmin_endpoint(self):
        response = self.client.get(
            reverse(
                "superadmin-organization-detail",
                kwargs={
                    "organization_id": self.organization.id,
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn(
            "Superadmin Operations MCP endpoint",
            body,
        )
        self.assertIn(
            "Superadmin MCP is managed globally",
            body,
        )
        self.assertIn(
            reverse("superadmin-operations-mcp"),
            body,
        )

    def test_global_workspace_can_revoke_superadmin_grant_and_close_context(self):
        token, support = self._superadmin_grant()

        response = self.client.post(
            reverse(
                "superadmin-operations-mcp-session-revoke",
                kwargs={"token_id": token.id},
            )
        )

        self.assertRedirects(
            response,
            reverse("superadmin-operations-mcp"),
        )
        token.refresh_from_db()
        support.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertIsNotNone(support.ended_at)

    def test_global_workspace_can_end_support_context_without_revoking_grant(self):
        token, support = self._superadmin_grant()

        response = self.client.post(
            reverse(
                "superadmin-operations-mcp-support-end",
                kwargs={"session_id": support.id},
            )
        )

        self.assertRedirects(
            response,
            reverse("superadmin-operations-mcp"),
        )
        token.refresh_from_db()
        support.refresh_from_db()
        self.assertIsNone(token.revoked_at)
        self.assertIsNone(token.active_organization_id)
        self.assertIsNotNone(support.ended_at)
