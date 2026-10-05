"""Exercise the real dashboard form and asynchronous Intelligence refresh."""
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import pytest
from django.template import engines
from django.template.loader import render_to_string
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CALL_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def call_browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("SHVYA_TEST_BROWSER"), args=["--no-sandbox"])
        yield browser.new_page()
        browser.close()


@pytest.mark.parametrize("final_status", ["completed", "failed"])
def test_save_notes_refreshes_intelligence_without_losing_editor_content(call_browser, final_status):
    page = call_browser
    call = NS(
        id=CALL_ID, contact_name="Outdated device contact", phone_number="+919876543210",
        lead=NS(name="CRM prospect", pipeline=NS(name="Sales"), stage=NS(name="Qualified")),
        user=NS(name="Agent", email="agent@example.com"), direction="outgoing", status="answered",
        provider="", notes="", transcript="", recording_url="", disposition="", follow_up_at=None,
        intelligence=None, analysis_status="not_requested", analysis_error="",
        talk_duration_seconds=102, ring_duration_seconds=0,
        ended_at=datetime(2026, 10, 5, 9, tzinfo=timezone.utc),
        get_direction_display=lambda: "Outgoing", get_status_display=lambda: "Answered",
        get_source_display=lambda: "Android SIM", get_analysis_status_display=lambda: "AI queued",
    )
    source = (ROOT / "templates/telephony/call_intelligence.html").read_text()
    content = source.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    html = engines["django"].from_string("{% load static call_metrics %}" + content).render({
        "active_section": "analytics", "recent_calls": [call], "is_admin": False,
        "call_page": NS(has_previous=False, has_next=False, number=1, paginator=NS(num_pages=1)),
        "stats": {}, "dispositions": [], "filters": {}, "devices": [], "agent_stats": [],
    })
    action_url = reverse("call-intelligence-call-action", args=[CALL_ID])
    status_url = reverse("call-intelligence-call-status", args=[CALL_ID])
    posts, polls, errors = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(r):
        path = urlsplit(r.request.url).path
        if path == action_url:
            posts.append(r.request.post_data)
            r.fulfill(json={"ok": True, "status_url": status_url})
        elif path == status_url:
            polls.append(path)
            call.analysis_status = "queued" if len(polls) == 1 else final_status
            if call.analysis_status == "completed":
                call.intelligence = NS(
                    summary="Prospect requested a demo next Tuesday.", intent="high", ai_score=8,
                    qualification_score=6, outcome="Demo requested", next_action="Arrange a demo",
                    objections=[], buying_signals=[], agent_metrics={}, compliance_flags=[],
                    get_intent_display=lambda: "High", get_sentiment_display=lambda: "Positive",
                )
            elif call.analysis_status == "failed":
                call.analysis_error = "AI is not configured. Ask your administrator to check the OpenAI configuration."
            r.fulfill(json={
                "ok": True, "analysis_status": call.analysis_status, "analysis_error": call.analysis_error,
                "badge_html": render_to_string("telephony/partials/intelligence_badge.html", {"call": call}),
                "intelligence_html": render_to_string("telephony/partials/intelligence_detail.html", {"call": call}),
            })
        else:
            r.fulfill(content_type="text/html", body=html)

    page.route("**/*", route)
    page.goto("http://localhost/")
    expect(page.locator(".ci-call-person strong")).to_have_text("CRM prospect")
    page.locator(".ci-call-row").click()
    notes = page.locator('[name="notes"]')
    notes.fill("Customer asked for a demo next Tuesday.")
    page.get_by_role("button", name="Save & analyze").click()
    if final_status == "completed":
        expect(page.locator(".ci-call-action-result")).to_have_text("Intelligence updated.")
        expect(page.locator("[data-ci-intelligence]")).to_contain_text("Prospect requested a demo next Tuesday.")
        expect(page.locator("[data-ci-intel-badge]")).to_contain_text("High · 8/10")
    else:
        expect(page.locator(".ci-call-action-result")).to_contain_text("administrator")
        expect(page.locator("[data-ci-intel-badge]")).to_contain_text("Analysis failed")
    expect(notes).to_have_value("Customer asked for a demo next Tuesday.")
    expect(page.get_by_role("button", name="Save & analyze")).to_be_enabled()
    assert len(posts) == 1
    assert len(polls) == 2
    assert not errors
