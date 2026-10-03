"""Real booking forms and scripts: native form property clobbering regression."""
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import pytest
from django.template.loader import render_to_string
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def calendar_browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("SHVYA_TEST_BROWSER"), args=["--no-sandbox"])
        page = browser.new_page()
        yield page
        browser.close()


def test_reschedule_and_move_post_to_booking_endpoint(calendar_browser):
    page = calendar_browser
    start = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
    booking = NS(id=ID, start_at=start, end_at=start, get_status_display="Scheduled",
                 page=NS(name="Demo", timezone="UTC"),
                 lead=NS(name="Jane", pipeline=NS(name="Sales"), stage=NS(name="New")))
    detail = render_to_string("shvya_calendar/booking_detail.html", {
        "booking": booking, "booking_zone": "UTC", "can_reschedule": True,
        "slot_date": "2026-10-05", "csrf_token": "test-csrf",
        "pipelines": [NS(id=ID, name="Support")],
        "stages": [NS(id=ID, pipeline_id=ID, name="Qualified")],
    })
    # Render the real workspace content without the unrelated application shell.
    from django.template import engines
    source = (ROOT / "templates/shvya_calendar/workspace.html").read_text()
    content = source.split('{% block content %}', 1)[1].split('{% endblock %}', 1)[0]
    shell = engines["django"].from_string(content).render({
        "calendar_timezone": "UTC", "calendar_today": "2026-10-05",
    })
    update_url = reverse("shvya_calendar:booking_update", kwargs={"booking_id": ID})
    detail_url = reverse("shvya_calendar:booking_detail", kwargs={"booking_id": ID})
    slots_url = reverse("shvya_calendar:booking_slots", kwargs={"booking_id": ID})
    events_url = reverse("shvya_calendar:events")
    posts, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(r):
        path = urlsplit(r.request.url).path
        if r.request.method == "POST":
            posts.append((path, r.request.post_data))
            r.fulfill(json={"ok": True} if path == update_url else {"error": "Wrong endpoint"}, status=200 if path == update_url else 404)
        elif path == events_url:
            r.fulfill(json={"events": [{"id": ID, "title": "Demo", "start": start.isoformat(), "end": "2026-10-05T10:30:00Z", "pipeline_id": ID, "pipeline": "Sales", "status": "scheduled", "detail_url": detail_url}], "next_offset": None})
        elif path == detail_url:
            r.fulfill(content_type="text/html", body=detail)
        elif path == slots_url:
            r.fulfill(json={"slots": [{"value": start.isoformat(), "label": "10:00 AM"}], "timezone": "UTC"})
        elif path == "/workspace.js":
            r.fulfill(content_type="application/javascript", body=(ROOT / "static/shvya_calendar/workspace.js").read_text())
        else:
            r.fulfill(content_type="text/html", body=shell + '<script src="/workspace.js"></script>')

    page.route("**/*", route)
    page.goto("http://localhost/")
    page.locator(".cw-event").click()
    page.get_by_text("Reschedule booking", exact=True).click()
    page.get_by_role("button", name="Find available times").click()
    page.locator('[name="slot_start"]').select_option(start.isoformat())
    page.get_by_role("button", name="Save new time").click()
    expect(page.locator("#cw-action-message")).to_have_text("Booking updated.")
    page.get_by_text("Move to another pipeline", exact=True).click()
    page.locator('[name="pipeline"]').select_option(ID)
    page.locator('[name="stage"]').select_option(ID)
    page.get_by_role("button", name="Move lead and bookings").click()
    expect(page.locator("#cw-action-message")).to_have_text("Booking updated.")
    assert len(posts) == 2
    assert all(path == update_url for path, _ in posts)
    assert 'reschedule' in posts[0][1] and 'move' in posts[1][1]
    assert all('test-csrf' in body for _, body in posts)
    assert errors == []


def test_copy_meeting_link_in_dynamically_inserted_panel(calendar_browser):
    page = calendar_browser
    link = "https://meet.google.com/abc-defg-hij"
    controls = render_to_string("shvya_calendar/partials/meeting_link.html", {"meeting_link": link})
    page.goto("about:blank")
    page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copied = text;}}})")
    page.add_script_tag(content=(ROOT / "static/shvya_calendar/meeting_link.js").read_text())
    page.evaluate("html => document.body.innerHTML = html", controls)
    page.get_by_role("button", name="Copy Meet link").click()
    expect(page.get_by_role("status")).to_have_text("Link copied.")
    assert page.evaluate("window.copied") == link
    expect(page.get_by_role("link", name="Join meeting")).to_have_attribute("href", link)
    page.evaluate("() => { navigator.clipboard.writeText = async () => {throw new Error('denied')}; }")
    page.get_by_role("button", name="Copy Meet link").click()
    expect(page.get_by_role("status")).to_contain_text("Unable to copy")
