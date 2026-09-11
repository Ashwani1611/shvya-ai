from django.test import RequestFactory
from django.urls import reverse

from apps.core.context_processors import sidebar_nav


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

    assert [item["label"] for item in workspace["items"]] == ["Sales Desk"]
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
