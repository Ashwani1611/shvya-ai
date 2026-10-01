"""Exercise the shipped campaign template/JS with all requests intercepted.

These checks require no server, database, WhatsApp session, or real recipients.
"""

import copy
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.template import Context, Engine
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "11111111-1111-4111-8111-111111111111"
TEMPLATE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("SHVYA_TEST_BROWSER")
            )
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed in this test environment.")
            raise
        yield browser
        browser.close()


@pytest.fixture
def campaign(browser):
    pages = []

    def create(*, media=True, attached=True, can_manage=True):
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        pages.append(page)
        control = {"previews": [], "confirmations": [], "uploads": [], "errors": [], "unexpected": []}
        page.on("pageerror", lambda error: control["errors"].append(str(error)))
        media_value = "asset:original" if attached else ""
        template = {
            "id": TEMPLATE, "name": "approved_offer", "category": "marketing",
            "language": "en", "body": "Hello {{name}}", "supported": True,
            "updated_at": "2026-09-30T12:00:00Z", "reason": "",
            "fields": [{"key": "body.name", "kind": "text", "label": "Name", "default": ""}],
            "bindings": {"body.name": {"source": "name", "default": ""}},
        }
        if media:
            template["fields"].insert(0, {
                "key": "header.media", "kind": "image", "label": "Image header", "default": media_value,
            })
            template["bindings"]["header.media"] = {"source": "", "default": media_value}
        paths = {name: f"/api/{name}/" for name in (
            "options", "data", "upload", "templates", "preview", "confirm", "media", "sample",
        )}
        bootstrap = {"urls": paths, "campaign_id": None, "open_composer": False}
        engine = Engine(
            loaders=[("django.template.loaders.locmem.Loader", {
                "base.html": """<!doctype html><html><head><meta charset="utf-8">
                {% block extra_head %}{% endblock %}</head><body>
                {% block content %}{% endblock %}</body></html>""",
                "campaign.html": (ROOT / "templates/channels/bulk_campaigns.html").read_text(),
            })],
            libraries={"static": "django.templatetags.static"},
        )
        html = engine.get_template("campaign.html").render(Context({
            "campaign_bootstrap": bootstrap, "csrf_token": "browser-test-token",
        }))

        def intercept(route):
            path = urlparse(route.request.url).path
            if path == "/campaigns/":
                route.fulfill(body=html, content_type="text/html")
            elif path in {"/static/channels/bulk_campaigns.js", "/static/channels/bulk_campaigns.css"}:
                route.fulfill(path=str(ROOT / path.lstrip("/")))
            elif path == paths["options"]:
                route.fulfill(json={
                    "can_create": True, "can_manage_template_media": can_manage,
                    "accounts": [{"id": ACCOUNT, "name": "Business", "business_name": "Business", "phone": "+919999999999"}],
                    "pipelines": [{"id": "sales", "name": "Sales", "stages": [{"id": "new", "name": "New"}]}],
                    "fields": [{"key": "phone", "label": "Phone", "required": True}, {"key": "name", "label": "Name"}],
                    "sources": [{"key": "name", "label": "Name"}],
                })
            elif path == paths["data"]:
                route.fulfill(json={
                    "summary": {}, "active": [], "active_count": 0, "history": [],
                    "pagination": {"total": 0, "number": 1, "pages": 1},
                    "retrieved_at": "2026-09-30T12:00:00Z",
                })
            elif path == paths["upload"]:
                route.fulfill(json={
                    "id": "audience-test", "filename": "audience.csv", "row_count": 1,
                    "headers": ["Phone", "Name"], "review_url": "/api/review/",
                })
            elif path == "/api/review/":
                route.fulfill(json={
                    "digest": "audience-digest", "errors": [],
                    "stats": {"rows": 1, "eligible": 1, "new": 1, "existing": 0, "invalid": 0, "duplicate": 0, "excluded": 0, "suppressed": 0},
                })
            elif path == paths["templates"]:
                route.fulfill(json={"templates": [template], "truncated": False})
            elif path == paths["preview"]:
                payload = route.request.post_data_json
                control["previews"].append(payload)
                bindings = payload["bindings"]
                ready = not media or bool(bindings.get("header.media", {}).get("default"))
                name = bindings["body.name"].get("default") or "Asha"
                route.fulfill(json={
                    "ready": ready, "digest": "preview-digest", "recipient_count": 1,
                    "missing_count": 0 if ready else 1,
                    "previews": [{"name": "Asha", "row": 2, "body": f"Hello {name}"}] if ready else [],
                    "errors": [] if ready else [{"row": 2, "reason": "Image header needs an attachment."}],
                })
            elif path == paths["media"]:
                control["uploads"].append(route.request.post_data_buffer)
                fields = copy.deepcopy(template["fields"])
                fields[0]["default"] = "asset:replacement"
                route.fulfill(json={
                    "fields": fields,
                    "bindings": {"header.media": {"source": "", "default": "asset:replacement"}},
                })
            elif path == paths["confirm"]:
                control["confirmations"].append(route.request.post_data_json)
                route.fulfill(json={"url": "/done/"})
            elif path == "/done/":
                route.fulfill(body="Campaign queued", content_type="text/html")
            else:
                control["unexpected"].append(route.request.url)
                route.abort()

        page.route("**/*", intercept)
        page.goto("https://campaign.test/campaigns/")
        expect(page.locator("#bulk-campaign-root")).to_have_attribute("aria-busy", "false")
        page.locator('[data-action="create"]').click()
        page.locator("#bc-file").set_input_files({
            "name": "audience.csv", "mimeType": "text/csv", "buffer": b"Phone,Name\n+919876543210,Asha\n",
        })
        expect(page.locator('[data-step="2"]')).to_be_visible()
        page.locator("#bc-pipeline").select_option("sales")
        page.locator("#bc-stage").select_option("new")
        page.locator("#bc-next").click()
        expect(page.locator('[data-step="3"]')).to_be_visible()
        page.locator("#bc-account").select_option(ACCOUNT)
        page.locator(f'[data-template="{TEMPLATE}"]').click()
        page.locator("#bc-next").click()
        expect(page.locator("#bc-next")).to_have_text("Send now")
        expect(page.locator("#bc-preview-status")).not_to_have_text("Checking actual recipient values…")
        page.locator("#bc-name").fill("Test broadcast")
        return page, control

    yield create
    for page in pages:
        page.close()


@pytest.mark.parametrize("media", [True, False], ids=["image-header", "text-only"])
def test_send_now_requires_preview_and_consent_then_confirms(campaign, media):
    page, control = campaign(media=media)
    expect(page.locator('#bc-bindings input[type="url"]')).to_have_count(0)
    expect(page.locator('#bc-bindings input[data-binding="header.media"]')).to_have_count(0)
    if media:
        expect(page.locator("#bc-bindings")).to_contain_text("Template attachment selected")
        assert control["previews"][-1]["bindings"]["header.media"]["default"] == "asset:original"
    else:
        expect(page.locator("[data-template-media]")).to_have_count(0)
        assert "header.media" not in control["previews"][-1]["bindings"]
    expect(page.locator("#bc-next")).to_be_disabled()
    page.locator("#bc-consent").check()
    expect(page.locator("#bc-next")).to_be_enabled()
    page.locator("#bc-next").click()
    page.wait_for_url("**/done/")
    assert len(control["confirmations"]) == 1
    assert control["confirmations"][0]["consent_confirmed"] is True
    assert control["confirmations"][0]["preview_digest"] == "preview-digest"
    assert control["errors"] == control["unexpected"] == []


def test_replacing_attachment_preserves_text_personalization(campaign):
    page, control = campaign()
    source = page.locator('[data-binding="body.name"][data-binding-kind="source"]')
    fallback = page.locator('[data-binding="body.name"][data-binding-kind="default"]')
    source.select_option("")
    fallback.fill("Customer")
    page.locator('[data-template-media="header.media"]').set_input_files({
        "name": "replacement.png", "mimeType": "image/png", "buffer": b"test image attachment",
    })
    expect(page.locator("#bc-preview-message")).to_have_text("Hello Customer")
    expect(source).to_have_value("")
    expect(fallback).to_have_value("Customer")
    page.locator("#bc-consent").check()
    expect(page.locator("#bc-next")).to_be_enabled()
    page.locator("#bc-next").click()
    page.wait_for_url("**/done/")
    assert len(control["uploads"]) == 1
    bindings = control["confirmations"][0]["bindings"]
    assert bindings["header.media"]["default"] == "asset:replacement"
    assert bindings["body.name"] == {"source": "", "default": "Customer"}
    assert control["errors"] == control["unexpected"] == []


def test_missing_media_keeps_send_disabled_until_attachment_uploaded(campaign):
    page, control = campaign(attached=False)
    page.locator("#bc-consent").check()
    expect(page.locator("#bc-next")).to_be_disabled()
    expect(page.locator("#bc-preview-status")).to_contain_text("Image header needs an attachment")
    assert control["confirmations"] == []
    page.locator('[data-template-media="header.media"]').set_input_files({
        "name": "attached.png", "mimeType": "image/png", "buffer": b"test image attachment",
    })
    expect(page.locator("#bc-preview-status")).to_contain_text("All 1 recipients have complete parameters")
    expect(page.locator("#bc-next")).to_be_enabled()
    assert control["previews"][-1]["bindings"]["header.media"]["default"] == "asset:replacement"
    assert control["errors"] == control["unexpected"] == []
