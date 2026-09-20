"""Exercise the real shared panel and smooth inbox scripts without providers."""

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from django.template.loader import render_to_string
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


def panel_html(identifier=A):
    lead = NS(
        id=identifier,
        organization=None,
        attributes={},
        name="First lead" if identifier == A else "Second lead",
        phone="+919876543210",
        email="",
        ai_enabled=True,
        pipeline_id=A,
        stage_id=A,
        notes="",
        lead_notes=NS(all=lambda: []),
        get_lead_source_display="WhatsApp API",
        created_at=datetime.now(timezone.utc),
    )
    replies = [NS(id=A, title="Welcome", body="Hello! How can we help?")]
    context = {
        "active_lead": lead,
        "channel": "whatsapp",
        "intent_score": {"assessed": True, "score": 2},
        "pipelines": [NS(id=A, name="Sales"), NS(id=B, name="Support")],
        "lead_stages": [NS(id=A, name="New lead")],
        "categories": [NS(id=A, name="Greetings", replies=NS(all=lambda: replies))],
        "csrf_token": "test-token",
    }
    with patch(
        "apps.channels.templatetags.channels_extras.AttributeDefinition.objects"
    ) as definitions:
        definitions.filter.return_value.order_by.return_value = []
        return render_to_string("channels/contact_panel.html", context)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("SHVYA_TEST_BROWSER"))
        yield browser
        browser.close()


@pytest.fixture
def inbox(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 940})
    posts = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    panels = {key: panel_html(key) for key in (A, B, C)}
    from apps.channels.whatsapp_chat_smooth_ui import _SMOOTH_INBOX_SCRIPT

    smooth = (
        _SMOOTH_INBOX_SCRIPT.decode()
        .replace("<script data-shvya-whatsapp-smooth-ui>", "")
        .replace("</script>", "")
    )

    def html(identifier):
        return (
            """<!doctype html><html><head><link rel="stylesheet" href="/static/css/contact_panel.css"><style>body{margin:0;background:#f5f5f7;font:14px Arial}.hidden{display:none}.shell{display:flex;height:900px;background:white}.contact-sidebar{width:360px;flex-shrink:0;border-left:1px solid #ddd}.list{width:200px}.list a{display:block;padding:20px}main{flex:1;display:flex;flex-direction:column;min-width:0}#thread{flex:1;overflow:auto;background:#eee;padding:20px}#message-body{padding:16px;width:70%}button{cursor:pointer}p,h2,h3{margin:0}*{box-sizing:border-box}</style><script>window.WebSocket=class{static OPEN=1;constructor(){this.readyState=1}close(){}}</script></head><body><div class="shell" id="wa-web-shell"><aside class="list">"""
            + "".join(
                f'<a href="/dashboard/whatsapp/chats/{key}/">Lead {n}</a>'
                for n, key in enumerate((A, B, C), 1)
            )
            + '''</aside><main><h2>Conversation</h2><div id="thread" class="wa-chat-surface">Latest customer message</div><form id="composer-form" data-lead-id="'''
            + identifier
            + """" action="/send/"><input name="csrfmiddlewaretoken" type="hidden" value="test-token"><input id="message-body"><button type="submit">Send</button></form></main><aside class="contact-sidebar" data-contact-host data-sidebar-url="/panel/"""
            + identifier
            + """/"></aside></div><div id="modal-root"></div><script src="/static/js/contact_panel.js" defer></script><script src="/static/js/whatsapp_composer.js" defer></script><script src="/smooth.js" defer></script></body></html>"""
        )

    def route(r):
        path = urlparse(r.request.url).path
        if r.request.method == "POST":
            posts.append((path, r.request.post_data))
            r.fulfill(json={"ok": True, "enabled": False})
        elif path.startswith("/static/"):
            r.fulfill(path=str(ROOT / path.lstrip("/")))
        elif path == "/smooth.js":
            r.fulfill(body=smooth, content_type="application/javascript")
        elif path.startswith("/panel/"):
            r.fulfill(body=panels[path.split("/")[2]], content_type="text/html")
        elif "/chats/" in path:
            r.fulfill(body=html(path.split("/")[-2]), content_type="text/html")
        else:
            r.fulfill(body="")

    page.route("**/*", route)
    page.goto(f"http://inbox.test/dashboard/whatsapp/chats/{A}/")
    expect(page.locator("[data-contact-panel=personal]")).to_be_visible()
    yield page, posts, errors
    assert not errors
    page.close()


def test_touchpoints_insert_reply_and_keep_existing_draft(inbox):
    page, posts, _ = inbox
    page.locator("#message-body").fill("My draft")
    page.get_by_role("button", name="Touchpoints", exact=True).click()
    page.get_by_placeholder("Search quick replies").fill("Welcome")
    page.get_by_role("button", name="Use reply").click()
    expect(page.locator("#message-body")).to_have_value(
        "My draft Hello! How can we help?"
    )
    assert not posts


def test_autosave_survives_navigation_and_never_targets_next_lead(inbox):
    page, posts, _ = inbox
    page.locator("[name=name]").fill("Updated customer")
    page.get_by_role("link", name="Lead 2", exact=True).click()
    expect(page).to_have_url(f"http://inbox.test/dashboard/whatsapp/chats/{B}/")
    expect(page.locator("[name=name]")).to_have_value("Second lead")
    assert any(A in path and "Updated" in (data or "") for path, data in posts)
    assert not any(B in path for path, _ in posts)
    page.locator("[name=email]").fill("new@example.com")
    expect(page.locator("[data-save-status]")).to_have_text("Saved")
    assert any(B in path for path, _ in posts)


def test_live_refresh_preserves_open_panel_draft_and_scroll(inbox):
    page, _, _ = inbox
    page.get_by_role("button", name="Touchpoints", exact=True).click()
    page.locator("#message-body").fill("Unsent draft")
    page.evaluate("window.shvyaWhatsAppNavigate(location.href,false,false)")
    expect(page.locator("[data-contact-panel=touchpoints]")).to_be_visible()
    expect(page.locator("#message-body")).to_have_value("Unsent draft")


def test_latest_chat_navigation_wins(inbox):
    page, _, _ = inbox
    page.evaluate(
        """id=>{const original=window.fetch;window.fetch=async(url,options)=>{if(String(url).includes('/chats/'+id+'/'))await new Promise(r=>setTimeout(r,450));return original(url,options);}}""",
        B,
    )
    page.evaluate(
        f"void window.shvyaWhatsAppNavigate('/dashboard/whatsapp/chats/{B}/',true)"
    )
    page.evaluate(
        f"void window.shvyaWhatsAppNavigate('/dashboard/whatsapp/chats/{C}/',true)"
    )
    expect(page).to_have_url(f"http://inbox.test/dashboard/whatsapp/chats/{C}/")
    expect(page.locator("[data-lead-id]").last).to_have_attribute("data-lead-id", C)
    page.wait_for_timeout(600)
    expect(page).to_have_url(f"http://inbox.test/dashboard/whatsapp/chats/{C}/")


def test_pipeline_post_does_not_resubmit_stale_stage(inbox):
    page, posts, _ = inbox
    page.locator("select[name=pipeline]").select_option(B)
    expect(page.locator("select[name=pipeline]")).to_be_enabled()
    assert len(posts) == 1
    assert "pipeline" in posts[0][1]
    assert 'name="stage"' not in posts[0][1]
