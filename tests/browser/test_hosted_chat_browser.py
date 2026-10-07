"""Real Chromium checks of the production Hosted inbox JS/CSS and template.

All network and WebSocket traffic is intercepted; these tests never connect a
real WhatsApp session or send a customer message.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from django.template import Context, Engine
from playwright.sync_api import Error as PlaywrightError
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
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(
                    "Playwright Chromium is not installed in this test environment."
                )
            raise
        yield browser
        browser.close()


@pytest.fixture
def inbox(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    rows = [
        {"key": A, "phone": A, "name": "First Lead", "stage_name": "New Lead", "unread": 2, "last_at": "2026-09-01T12:03:00Z", "last_message": "Hello"},
        {"key": B, "phone": B, "name": "Second Lead", "stage_name": "Qualified", "unread": 0, "last_at": "2026-09-01T12:02:00Z", "last_message": "Hi"},
    ]
    control = {"delay": None, "pending": [], "requests": [], "receipts": [], "status": "connected", "older": False, "media": False, "tick": "sent", "extra": [], "errors": []}
    page.on("pageerror", lambda error: control["errors"].append(str(error)))
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
        if chat == A and not before:
            messages += control["extra"]
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
        elif url.path.endswith(("hosted_whatsapp_chat.js", "hosted_chat_live_state.js")):
            request.fulfill(body=(ROOT / "static/js" / Path(url.path).name).read_text(), content_type="application/javascript; charset=utf-8")
        elif url.path.endswith("contact_panel.js"):
            request.fulfill(body="", content_type="application/javascript; charset=utf-8")
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


def push(control, item, *, chat=A, operation="upsert", conversation=None):
    control["socket"].send(json.dumps({
        "kind": "message", "account_id": ACCOUNT, "chat_key": chat,
        "aliases": [chat], "operation": operation,
        "message_id": item["id"], "updated_at": item["updated_at"],
        "message": item, "conversation": conversation,
    }))


def test_live_inbound_and_ai_bubbles_do_not_wait_for_snapshot_response(inbox):
    page, control = inbox
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-scroll .bubble")).to_have_count(60)
    control["delay"] = A
    control["socket"].send('{"kind":"refresh","reason":"message"}')
    page.wait_for_timeout(250)
    assert control["pending"]
    inbound = message(181, body="Live inbound without refresh", updated_at="2026-10-03T00:00:01Z")
    push(control, inbound)
    expect(page.locator('[data-message-id="message-0181"]')).to_contain_text(inbound["body"], timeout=2000)
    reply = message(182, body="Live AI reply", direction="outbound", status="queued", updated_at="2026-10-03T00:00:02Z")
    push(control, reply)
    bubble = page.locator('[data-message-id="message-0182"]')
    expect(bubble.locator(".bubble-status")).to_contain_text("Queued", timeout=2000)
    bubble.evaluate("node => node.dataset.keep = 'same-node'")
    for index, status in enumerate(("sending", "sent", "read"), start=3):
        reply = {**reply, "status": status, "updated_at": f"2026-10-03T00:00:0{index}Z"}
        push(control, reply)
        expect(bubble.locator(".bubble-status")).to_have_attribute("title", {
            "sending": "Sending — awaiting confirmation", "sent": "Sent", "read": "Read",
        }[status])
        expect(bubble).to_have_attribute("data-keep", "same-node")
    push(control, reply)
    push(control, {**reply, "status": "sent", "updated_at": "2026-10-03T00:00:03Z"})
    expect(bubble).to_have_count(1)
    expect(bubble.locator(".bubble-status")).to_have_attribute("title", "Read")
    pending, stale = control["pending"].pop(0)
    pending.fulfill(json=stale)
    page.wait_for_timeout(200)
    expect(bubble.locator(".bubble-status")).to_have_attribute("title", "Read")
    expect(page.locator('[data-message-id="message-0181"]')).to_have_count(1)
    assert control["errors"] == []


def test_other_chat_delta_updates_sidebar_without_leaking_into_open_thread(inbox):
    page, control = inbox
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-scroll .bubble")).to_have_count(60)
    control["delay"] = A
    item = message(183, body="Other conversation live", updated_at="2026-10-03T00:00:01Z")
    push(control, item, chat=B, conversation={
        "key": B, "name": "Second Lead", "phone": B, "last_message": item["body"],
        "last_at": item["created_at"], "last_message_id": item["id"],
    })
    expect(page.locator(f'[data-chat-key="{B}"] .hosted-conversation-preview')).to_have_text(item["body"], timeout=2000)
    expect(page.locator('#thread-scroll [data-message-id="message-0183"]')).to_have_count(0)


def test_missed_socket_event_is_recovered_even_when_socket_is_open(inbox):
    page, control = inbox
    page.locator(f'[data-chat-key="{A}"]').click()
    expect(page.locator("#thread-scroll .bubble")).to_have_count(60)
    item = message(184, body="Recovered missed live event")
    control["extra"].append(item)
    expect(page.locator('[data-message-id="message-0184"]')).to_contain_text(item["body"], timeout=5000)


def test_cancelled_draft_is_removed_by_direct_event(inbox):
    page, control = inbox
    item = message(185, body="Draft to be cancelled", direction="outbound", status="queued", updated_at="2026-10-03T00:00:01Z")
    control["extra"].append(item)
    page.locator(f'[data-chat-key="{A}"]').click()
    bubble = page.locator('[data-message-id="message-0185"]')
    expect(bubble).to_have_count(1)
    control["delay"] = A
    push(control, {**item, "updated_at": "2026-10-03T00:00:02Z"}, operation="remove")
    expect(bubble).to_have_count(0, timeout=2000)


def test_delayed_socket_event_cannot_requeue_message_loaded_as_read(inbox):
    page, control = inbox
    item = message(186, body="Already read on recipient phone", direction="outbound",
                   status="read", updated_at="2026-10-07T00:00:03Z")
    control["extra"].append(item)
    page.locator(f'[data-chat-key="{A}"]').click()
    bubble = page.locator('[data-message-id="message-0186"]')
    expect(bubble.locator(".bubble-status")).to_have_attribute("title", "Read")
    control["delay"] = A
    push(control, {**item, "status": "queued", "updated_at": "2026-10-07T00:00:01Z"})
    page.wait_for_timeout(200)
    expect(bubble.locator(".bubble-status")).to_have_attribute("title", "Read")
    assert control["errors"] == []
