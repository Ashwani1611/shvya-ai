"""Exercise the real dashboard form and asynchronous Intelligence refresh."""
import os
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.template import engines
from django.core.paginator import Paginator
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


def activity_workspace(page, *, reject_creation=False):
    """Render the production templates/assets; mock only the HTTP boundary."""
    def make_call(identifier, name, *, outcome="", linked=False, agent="Priya Sharma"):
        call = NS(
            id=identifier, contact_name=name, phone_number="+91987654321" + identifier[-1],
            lead=None, lead_id=None, user=NS(name=agent, email="agent@example.com"),
            direction="outgoing", status="answered", provider="", notes="", transcript="",
            recording_url="", disposition=outcome, follow_up_at=None, intelligence=None,
            analysis_status="not_requested", analysis_error="", crm_url="",
            outcome_label={"": "Not classified", "interested": "Interested", "no_answer": "No Answer"}[outcome],
            outcome_category={"": "unclassified", "interested": "connected", "no_answer": "not_connected"}[outcome],
            outcome_is_active=True,
            talk_duration_seconds=102, ring_duration_seconds=10,
            ended_at=datetime(2026, 10, 5, 9, tzinfo=timezone.utc),
            get_direction_display=lambda: "Outgoing", get_status_display=lambda: "Answered",
            get_source_display=lambda: "Android SIM", get_analysis_status_display=lambda: "Add notes",
        )
        if linked:
            call.lead_id = "55555555-5555-4555-8555-55555555555" + identifier[-1]
            call.lead = NS(id=call.lead_id, name=name, pipeline=NS(name="Sales"), stage=NS(name="Qualified"))
            call.crm_url = reverse("crm-dashboard") + "?" + urlencode({"pipeline": "sales", "stage": "qualified", "lead": call.lead_id})
        return call

    calls = [
        make_call(CALL_ID, "Ananya Mehta", outcome="interested", linked=True, agent="Gaurav Singh"),
        make_call(CALL_ID[:-1] + "2", "Rahul Shah"),
        make_call(CALL_ID[:-1] + "3", "Sana Kapoor", outcome="no_answer", linked=True),
    ]
    calls[0].analysis_status = "completed"
    calls[0].intelligence = NS(
        summary="Requested a product demo next Tuesday.", intent="high", ai_score=8,
        qualification_score=6, outcome="Demo requested", next_action="Arrange a demo",
        objections=[], buying_signals=[], agent_metrics={}, compliance_flags=[],
        get_intent_display=lambda: "High", get_sentiment_display=lambda: "Positive",
    )
    destinations = [
        {"id": "sales", "name": "Sales", "owned": True, "stages": [{"id": "new", "name": "New Lead"}, {"id": "qualified", "name": "Qualified"}]},
        {"id": "visits", "name": "Visits", "owned": False, "stages": [{"id": "booked", "name": "Visit Booked"}]},
    ]
    posts, errors = [], []
    page.on("pageerror", lambda exception: errors.append(str(exception)))
    template = (ROOT / "templates/telephony/call_intelligence.html").read_text()
    content = template.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    css = (ROOT / "static/telephony/call-intelligence.css").read_text() + (ROOT / "static/telephony/call-activity.css").read_text()
    script = (ROOT / "static/telephony/call-activity.js").read_text()
    dashboard = reverse("call-intelligence-dashboard")

    def render(params):
        selected = params.get("disposition", [""])[0]
        crm = params.get("crm", [""])[0]
        lead_id = params.get("lead", [""])[0]
        base = [call for call in calls if not lead_id or str(call.lead_id) == lead_id]
        matching = [call for call in base if
                    (not selected or (not call.disposition if selected == "unclassified" else call.disposition == selected))
                    and (not crm or bool(call.lead) == (crm == "linked"))]
        groups = []
        for code, name, category in [("", "All calls", "all"), ("unclassified", "Not classified", "unclassified"),
                                     ("interested", "Interested", "connected"), ("no_answer", "No Answer", "not_connected")]:
            scoped = [call for call in base if not crm or bool(call.lead) == (crm == "linked")]
            total = sum(not code or (not call.disposition if code == "unclassified" else call.disposition == code) for call in scoped)
            query = {"section": "analytics", "disposition": code}
            if crm:
                query["crm"] = crm
            groups.append({"code": code, "name": name, "category": category, "total": total,
                           "selected": code == selected, "url": dashboard + "?" + urlencode(query) + "#calls"})
        crm_groups = [
            {"code": code, "name": name, "total": sum(not code or bool(call.lead) == (code == "linked") for call in base),
             "selected": code == crm, "url": dashboard + "?" + urlencode({"section": "analytics", "crm": code}) + "#calls"}
            for code, name in [("", "All contacts"), ("linked", "CRM leads"), ("unlinked", "Needs a lead")]
        ]
        html = engines["django"].from_string("{% load static call_metrics %}" + content).render({
            "active_section": "analytics", "recent_calls": matching, "is_admin": False,
            "call_page": Paginator(matching, 50).get_page(1), "stats": {},
            "dispositions": [NS(code="interested", name="Interested", category="connected"),
                             NS(code="no_answer", name="No Answer", category="not_connected")],
            "filters": {}, "devices": [], "agent_stats": [], "outcome_groups": groups,
            "active_outcome": next(group["name"] for group in groups if group["selected"]),
            "crm_groups": crm_groups, "can_create_call_lead": True, "lead_destinations": destinations,
            "contact_history_name": matching[0].lead.name if lead_id and matching else "",
        })
        return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta charset="utf-8"><style>' + css + '\nbody{margin:0;background:#f5f5f7}.ci-page{margin:0 auto;padding:24px;max-width:1300px}'
                '</style></head><body>' + html + '<script>' + script + '</script></body></html>')

    def route(request_route):
        request = request_route.request
        path = urlsplit(request.url).path
        if request.method == "POST":
            payload = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                + request.post_data_buffer
            )
            data = {part.get_param("name", header="content-disposition"): part.get_content().strip()
                    for part in payload.iter_parts()}
            posts.append(data)
            if path == reverse("call-intelligence-call-action", args=[calls[1].id]):
                call = calls[1]
                call.disposition = data["disposition"]
                call.outcome_label = "Interested"
                call.outcome_category = "connected"
                call.notes = data["notes"]
                request_route.fulfill(json={"ok": True, "outcome_changed": True,
                                           "activity_url": dashboard + "?" + urlencode({
                                               "section": "analytics", "disposition": call.disposition, "open": call.id,
                                           }) + "#calls"})
                return
            if reject_creation:
                request_route.fulfill(status=400, json={"ok": False, "error": "Choose an active stage from the selected pipeline."})
                return
            call = calls[1]
            destination = next(item for item in destinations if item["id"] == data["pipeline"])
            stage = next(item for item in destination["stages"] if item["id"] == data["stage"])
            call.lead_id = "55555555-5555-4555-8555-555555555552"
            call.lead = NS(id=call.lead_id, name=data["name"], pipeline=NS(name=destination["name"]), stage=NS(name=stage["name"]))
            call.crm_url = reverse("crm-dashboard") + "?" + urlencode({"pipeline": destination["id"], "stage": stage["id"], "lead": call.lead_id})
            request_route.fulfill(json={"ok": True, "created": True, "redirect_url": dashboard + "?" + urlencode({"section": "analytics", "lead": call.lead_id, "open": call.id}) + "#calls"})
        elif path == dashboard:
            request_route.fulfill(content_type="text/html; charset=utf-8", body=render(parse_qs(urlsplit(request.url).query)))
        else:
            request_route.fulfill(status=404)

    page.route("**/*", route)
    page.goto("http://localhost" + dashboard + "?section=analytics#calls")
    return posts, errors


def test_outcome_navigation_opens_matching_calls_and_all_intelligence(call_browser):
    page = call_browser
    _, errors = activity_workspace(page)
    page.get_by_role("navigation", name="Call outcomes").get_by_role("link", name="Interested", exact=False).click()
    expect(page.locator(".ci-activity-card")).to_have_count(1)
    expect(page.locator(".ci-call-agent")).to_contain_text("Gaurav Singh")
    expect(page.locator(".ci-call-crm")).to_contain_text("Qualified")
    expect(page.locator(".ci-call-person")).to_contain_text("Ananya Mehta")
    page.locator(".ci-call-row").click()
    expect(page.locator("[data-ci-intelligence]")).to_contain_text("Requested a product demo next Tuesday.")
    expect(page.get_by_role("link", name="View lead in CRM")).to_have_attribute("href", "/dashboard/?pipeline=sales&stage=qualified&lead=55555555-5555-4555-8555-555555555551")
    assert not errors


def test_create_lead_selects_pipeline_stage_and_displays_linked_contact(call_browser):
    page = call_browser
    page.set_viewport_size({"width": 375, "height": 900})
    posts, errors = activity_workspace(page)
    page.get_by_role("navigation", name="CRM lead status").get_by_role("link", name="Needs a lead").click()
    expect(page.locator(".ci-activity-card")).to_have_count(1)
    page.get_by_role("button", name="Create lead", exact=True).click()
    dialog = page.get_by_role("dialog", name="Create lead from this call")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[name="name"]')).to_have_value("Rahul Shah")
    expect(dialog.locator('[name="phone"]')).to_have_attribute("readonly", "")
    dialog.get_by_role("combobox", name="Lead pipeline").select_option("visits")
    expect(dialog.get_by_role("combobox", name="Lead stage").locator("option")).to_have_text(["Select stage", "Visit Booked"])
    dialog.get_by_role("combobox", name="Lead stage").select_option("booked")
    bounds = dialog.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 375
    path = os.environ.get("CALL_ACTIVITY_QA_PATH")
    if path:
        dialog.screenshot(path=path + "/create-lead-mobile.png")
    dialog.get_by_role("button", name="Create lead & link call").click()
    expect(page.locator(".ci-call-crm")).to_contain_text("CRM lead")
    expect(page.locator(".ci-call-crm")).to_contain_text("Visits")
    expect(page.locator(".ci-call-crm")).to_contain_text("Visit Booked")
    expect(page.locator(".ci-call-agent")).to_contain_text("Priya Sharma")
    expect(page.get_by_role("link", name="View lead in CRM")).to_have_attribute("href", "/dashboard/?pipeline=visits&stage=booked&lead=55555555-5555-4555-8555-555555555552")
    assert posts[0]["pipeline"] == "visits"
    assert posts[0]["stage"] == "booked"
    assert len(posts) == 1
    assert not errors


def test_create_lead_validation_preserves_form_and_allows_retry(call_browser):
    page = call_browser
    posts, errors = activity_workspace(page, reject_creation=True)
    page.get_by_role("button", name="Create lead", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("combobox", name="Lead stage").select_option("new")
    dialog.get_by_role("button", name="Create lead & link call").click()
    expect(dialog.get_by_role("alert")).to_contain_text("selected pipeline")
    expect(dialog.locator('[name="name"]')).to_have_value("Rahul Shah")
    expect(dialog.get_by_role("button", name="Create lead & link call")).to_be_enabled()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    assert len(posts) == 1
    assert not errors


def test_saving_new_outcome_refreshes_groups_and_reopens_original_call(call_browser):
    page = call_browser
    posts, errors = activity_workspace(page)
    outcomes = page.get_by_role("navigation", name="Call outcomes")
    outcomes.get_by_role("link", name="Not classified").click()
    page.locator(".ci-call-row").click()
    page.locator('[name="notes"]').fill("Customer requested a demo.")
    page.locator('.ci-post-call-form [name="disposition"]').select_option("interested")
    page.get_by_role("button", name="Save & analyze").click()
    expect(page.locator('.ci-outcome-stage[aria-current="page"]')).to_contain_text("Interested")
    expect(page.locator('.ci-outcome-stage[aria-current="page"]')).to_contain_text("2 calls")
    expect(page.locator(".ci-activity-card")).to_have_count(2)
    expect(page.locator("#call-" + CALL_ID[:-1] + "2")).to_have_attribute("open", "")
    expect(page.locator("#call-" + CALL_ID[:-1] + "2").locator('[name="notes"]')).to_have_value("Customer requested a demo.")
    assert len(posts) == 1
    assert not errors


@pytest.mark.parametrize("width", [1440, 375])
def test_workspace_layout_keeps_crm_and_user_visible_on_mobile(call_browser, width):
    page = call_browser
    page.set_viewport_size({"width": width, "height": 1000})
    _, errors = activity_workspace(page)
    card = page.locator(".ci-activity-card").first
    expect(card.locator(".ci-call-agent")).to_be_visible()
    expect(card.locator(".ci-call-crm")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    path = os.environ.get("CALL_ACTIVITY_QA_PATH")
    if path:
        page.locator("#calls").screenshot(path=path + f"/calls-{width}.png")
    assert not errors
