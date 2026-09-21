from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.core.context_processors import sidebar_nav
from apps.accounts.models import User
from apps.integrations.models import (
    OperationsOAuthClient,
    OperationsOAuthToken,
    OperationsSupportSession,
)
from apps.integrations.operations_auth import token_hash
from apps.integrations.operations_policy import ROLE_SUPERADMIN
from apps.organizations.models import Organization


def test_sidebar_navigation_is_grouped_for_premium_shell():
    request = RequestFactory().get("/dashboard/workflows/")
    context = sidebar_nav(request)

    sections = context["sidebar_nav_sections"]
    assert [section["key"] for section in sections] == [
        "workspace",
        "customers",
        "automate",
        "connect",
    ]

    workspace = sections[0]
    customers = sections[1]
    automate = sections[2]
    connect = sections[3]

    assert [item["label"] for item in workspace["items"]] == ["Sales Desk", "SHVYA Sales"]
    assert [item["label"] for item in customers["items"]] == ["CRM", "Insights"]
    assert [item["label"] for item in automate["items"]] == [
        "Cadence",
        "Playbooks",
        "Workflows",
    ]
    assert [item["label"] for item in connect["items"]] == [
        "WhatsApp",
        "Instagram",
        "Connect Hub",
    ]


def test_sidebar_context_resolves_links_and_footer_utilities():
    request = RequestFactory().get("/dashboard/")
    context = sidebar_nav(request)

    customers = next(
        section
        for section in context["sidebar_nav_sections"]
        if section["key"] == "customers"
    )
    crm = next(item for item in customers["items"] if item["label"] == "CRM")

    assert crm["href"] == reverse("crm-dashboard")
    assert crm["is_active"]

    utilities = context["sidebar_utility_items"]
    assert [item["sidebar_label"] for item in utilities if "sidebar_label" in item] == [
        "Help & Support"
    ]
    assert utilities[-1]["label"] == "Settings"
    assert utilities[-1]["href"] == reverse("crm-profile")


def test_shvya_sales_sidebar_link_and_active_state():
    request = RequestFactory().get("/dashboard/sales/")
    context = sidebar_nav(request)

    workspace = next(
        section
        for section in context["sidebar_nav_sections"]
        if section["key"] == "workspace"
    )
    sales = next(item for item in workspace["items"] if item["label"] == "SHVYA Sales")

    assert sales["href"] == reverse("shvya-sales-dashboard")
    assert sales["is_active"]


@pytest.mark.django_db
def test_sidebar_context_exposes_active_superadmin_operations_support():
    organization = Organization.objects.create(name="Support Presence Org")
    admin = User.objects.create_user(
        email="presence-admin@example.test",
        organization=organization,
        password=None,
        name="Org Admin",
        role=User.Role.ADMIN,
    )
    superadmin = User.objects.create_superuser(
        email="presence-support@example.test",
        password=None,
        name="SHVYA Support",
    )
    client = OperationsOAuthClient.objects.create(
        client_id="presence-client",
        client_name="Presence test",
        redirect_uris=["https://chatgpt.com/aip/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    token = OperationsOAuthToken.objects.create(
        client=client,
        actor=superadmin,
        role=ROLE_SUPERADMIN,
        access_token_hash=token_hash("presence-access"),
        refresh_token_hash=token_hash("presence-refresh"),
        scope="operations.read operations.write",
        resource="http://testserver/operations/mcp/",
        active_organization=organization,
        expires_at=timezone.now() + timedelta(hours=1),
        refresh_expires_at=timezone.now() + timedelta(days=1),
    )
    OperationsSupportSession.objects.create(
        token=token,
        actor=superadmin,
        organization=organization,
        reason="Investigate organization configuration",
    )

    request = RequestFactory().get("/dashboard/")
    request.crm_user = admin
    context = sidebar_nav(request)

    assert context["operations_support_active"] is True
    assert context["operations_support_actor"] == "SHVYA Support"
    assert context["operations_support_started_at"] is not None


@pytest.mark.django_db
def test_sidebar_context_hides_expired_operations_support():
    organization = Organization.objects.create(name="Expired Support Org")
    admin = User.objects.create_user(
        email="expired-admin@example.test",
        organization=organization,
        password=None,
        name="Org Admin",
        role=User.Role.ADMIN,
    )
    superadmin = User.objects.create_superuser(
        email="expired-support@example.test",
        password=None,
        name="SHVYA Support",
    )
    client = OperationsOAuthClient.objects.create(
        client_id="expired-presence-client",
        client_name="Expired presence test",
        redirect_uris=["https://chatgpt.com/aip/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    token = OperationsOAuthToken.objects.create(
        client=client,
        actor=superadmin,
        role=ROLE_SUPERADMIN,
        access_token_hash=token_hash("expired-presence-access"),
        refresh_token_hash=token_hash("expired-presence-refresh"),
        scope="operations.read operations.write",
        resource="http://testserver/operations/mcp/",
        active_organization=organization,
        expires_at=timezone.now() - timedelta(minutes=1),
        refresh_expires_at=timezone.now() + timedelta(days=1),
    )
    OperationsSupportSession.objects.create(
        token=token,
        actor=superadmin,
        organization=organization,
        reason="Expired support session",
    )

    request = RequestFactory().get("/dashboard/")
    request.crm_user = admin
    context = sidebar_nav(request)

    assert context["operations_support_active"] is False
    assert context["operations_support_actor"] == ""
