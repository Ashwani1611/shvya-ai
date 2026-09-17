"""Real Chromium checks of the production Hosted inbox JS/CSS and template.

All network and WebSocket traffic is intercepted; these tests never connect a
real WhatsApp session or send a customer message.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from django.template import Context, Engine
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "11111111-1111-4111-8111-111111111111"
A = "+919812345678"
B = "+919887654321"


def message(index, *, body=None, **kwargs):
    return {
        "id": f"message-{index:04d}", "body": body or f"Message {index}",
        "created_at": datetime(2026, 9, 1, 12, index // 60, index % 60, tzinfo=timezone.utc).isoformat(),
        "direction": "inbound", "status": "received", "message_type": "text", **kwargs,
    }


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def inbox(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    rows = [
        {"key": A, "phone": A, "name": "First Lead", "stage_name": "New Lead", "unread": 2, "last_at": "2026-09-01T12:03:00Z", "last_message": "Hello"},
        {"key": B, "phone": B, "name": "Second Lead", "stage_name": "Qualified", "unread": 0, "last_at": "2026-09-01T12:02:00Z", "last_message": "Hi"},
    ]
    control = {"delay": None, "pending": [], "requests": [], "receipts": [], "status": "connected", "older": False, "media": False, "tick": "sent"}
    template = (ROOT / "templates/channels/hosted_whatsapp_chats.html").read_text()
    engine = Engine(
        loaders=[("django.template.loaders.locmem.Loader", {
            # Mirror base_legacy.html's sidebar-adjacent flex column and main
            # viewport. Without these parents the thread grows with its messages
            # rather than scrolling, so scroll-anchor assertions are meaningless.
            "base.html": '''<!doctype html><html><head><meta charset="utf-8">
                <meta name="viewport" content="width=device-width">
                <style>html,body{margin:0;height:100%}*{box-sizing:border-box}</style>
                </head><body><aside id="app-sidebar"></aside>
                <div style="display:flex;flex-direction:column;overflow:hidden">
                <header style="flex-shrink:0"></header>
                <main>{% block content %}{% endblock %}</main>
                </div></body></html>''',
            "chat.html": template,
        })],
        libraries={"static": "django.templatetags.static"},
    )
    html = engine.get_template("chat.html").render(Context({
        "account": SimpleNamespace(id=ACCOUNT, status="connected", display_phone_number="+919319988591"),
        "conversations": rows, "selected_chat": "", "selected_name": "", "thread": [],
        "search_query": "", "csrf_token": "test-token",
    }))

    def snapshot(chat, before=False):
        messages = [message(i) for i in range(60 if before else 120, 120 if before else 180)] if chat else []
        if chat == B:
            messages = [message(180, body="Second conversation")]
        if control["media"] and chat == A:
            messages = [message(1, message_type="video", media_url="/video.mp4", direction="outbound", status=control["tick"])]
        return {
            "ok": True, "conversations": rows, "selected_chat": chat,
            "selected_name": "First Lead" if chat == A else "Second Lead",
            "thread": messages, "account_status": control["status"],
            "read_token": "receipt-" + chat if chat else "",
            "has_more": bool(control["older"] and not before),
            "next_before": "older-cursor" if control["older"] and not before else "",
        }

    def route(request):
        url = urlparse(request.request.url)
        if url.path.endswith("/data/"):
            params = parse_qs(url.query)
            chat = params.get("chat", [""])[0]
            control["requests"].append(chat)
            payload = snapshot(chat, "before" in params)
            if control["delay"] == chat:
                control["pending"].append((request, payload))
            else:
                request.fulfill(json=payload)
        elif url.path.endswith("/read/"):
            control["receipts"].append(request.request.post_data_json)
            rows[0]["unread"] = 0
            request.fulfill(json={"ok": True, "marked_read": 2})
        elif url.path.endswith("hosted_whatsapp_chat.js"):
            request.fulfill(body=(ROOT / "static/js/hosted_whatsapp_chat.js").read_text(), content_type="application/javascript; charset=utf-8")
        elif url.path.endswith("hosted_whatsapp_chat.css"):
            request.fulfill(body=(ROOT / "static/css/hosted_whatsapp_chat.css").read_text(), content_type="text/css; charset=utf-8")
        elif url.path == "/video.mp4":
            request.fulfill(status=204)
        else:
            request.fulfill(body=html, content_type="text/html; charset=utf-8")

    page.route("**/*", route)
    page.route_web_socket("**/ws/**", lambda socket: control.update(socket=socket))
    page.goto("https://hosted.test/inbox/")
    expect(page.locator(".hosted-conversation")).to_have_count(2)
    page.wait_for_function("document.querySelector('#hosted-live-status').textContent.includes('Live')")
    yield page, control
    page.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_hidden_empty_panel_does_not_occupy_open_thread_or_draw_green_line(inbox, width):
    page, _ = inbox
    page.set_viewport_size({"width": width, "height": 900})
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-scroll .bubble")).to_have_count(60)
    expect(page.locator("#thread-empty")).to_be_hidden()
    assert page.locator("#thread-empty").evaluate("el => getComputedStyle(el).borderBottomWidth") == "0px"
    expect(page.locator("#chat-form")).to_be_visible()
    box = page.locator("#thread-scroll").bounding_box()
    assert 300 < box["height"] < page.viewport_size["height"]


def test_slow_old_chat_response_cannot_overwrite_latest_selection(inbox):
    page, control = inbox
    control["delay"] = A
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-name")).to_have_text("First Lead")
    expect(page.locator(".thread-loading")).to_be_visible()
    page.locator(f'[data-chat-key="{B}"]').click()
    expect(page.locator("#thread-name")).to_have_text("Second Lead")
    expect(page.locator("#thread-scroll")).to_contain_text("Second conversation")
    for pending, payload in control["pending"]:
        pending.fulfill(json=payload)
    expect(page.locator("#thread-name")).to_have_text("Second Lead")
    expect(page.locator("#thread-scroll .bubble")).to_have_count(1)


def test_stage_and_confirmed_read_receipt_use_server_state(inbox):
    page, control = inbox
    expect(page.locator(f'[data-chat-key="{B}"] .hosted-stage')).to_have_text("Qualified")
    assert control["receipts"] == []
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator(f'[data-chat-key="{A}"] .hosted-unread')).to_have_count(0)
    assert control["receipts"] == [{"token": "receipt-" + A}]


def test_older_messages_keep_scroll_anchor_and_survive_realtime_refresh(inbox):
    page, control = inbox
    control["older"] = True
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-scroll .bubble")).to_have_count(60)
    page.locator("#thread-scroll").evaluate("el => el.scrollTop = 150")
    before = page.locator('[data-message-id="message-0120"]').bounding_box()["y"]
    page.locator(".hosted-load-older").evaluate("el => el.click()")
    expect(page.locator("#thread-scroll .bubble")).to_have_count(120)
    after = page.locator('[data-message-id="message-0120"]').bounding_box()["y"]
    assert abs(after - before) < 4
    control["socket"].send('{"kind":"refresh","reason":"message"}')
    page.wait_for_timeout(250)
    expect(page.locator("#thread-scroll .bubble")).to_have_count(120)


def test_delivery_tick_does_not_recreate_video_node(inbox):
    page, control = inbox
    control["media"] = True
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("video")).to_have_count(1)
    page.locator("video").evaluate("el => el.dataset.keep = 'original'")
    control["tick"] = "read"
    control["socket"].send('{"kind":"refresh","reason":"status"}')
    expect(page.locator(".bubble-status.read")).to_be_visible()
    expect(page.locator("video")).to_have_attribute("data-keep", "original")


def test_socket_failure_uses_polling_without_claiming_whatsapp_disconnected(inbox):
    page, control = inbox
    control["socket"].close(code=1011, reason="test transient socket outage")
    expect(page.locator("[data-live-label]")).to_have_text("Connected · polling")
