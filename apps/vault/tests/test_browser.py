"""Native form regressions using synthetic pages routed in memory."""
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse
from playwright.sync_api import sync_playwright

from apps.organizations.models import Organization
from apps.vault.models import Vault

from .helpers import VaultTestCase


@override_settings(
    DEBUG=False, SECURE_SSL_REDIRECT=True, SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True, CSRF_USE_SESSIONS=False, CSRF_TRUSTED_ORIGINS=[],
)
class VaultBrowserFormTests(VaultTestCase):
    origin = "https://testserver"
    file_bytes = b"Synthetic Vault browser upload."

    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)

    def hidden_fields(self, response, selector):
        self.assertEqual(response.status_code, 200)
        form = BeautifulSoup(response.content, "html.parser").select_one(selector)
        self.assertIsNotNone(form)
        data = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"][name]')}
        self.assertTrue(data.get("csrfmiddlewaretoken"))
        return data

    def native_submit(self, document_path, response, target_path, kind, section):
        """Capture Chromium's actual action, multipart body, cookies and CSRF headers."""
        posted = []
        script = (Path(settings.BASE_DIR) / "static/vault/vault.js").read_text()

        def route_request(route):
            request = route.request
            if request.method == "POST" and request.url.startswith(self.origin + "/"):
                posted.append((request.url, request.post_data_buffer, request.all_headers()))
                route.fulfill(status=200, content_type="text/html", body="Submission captured")
            elif urlsplit(request.url).path == document_path:
                route.fulfill(status=200, headers=dict(response.items()), body=response.content)
            elif urlsplit(request.url).path == "/static/vault/vault.js":
                route.fulfill(status=200, content_type="application/javascript", body=script)
            else:
                route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=shutil.which("chromium") or shutil.which("google-chrome"),
            )
            try:
                context = browser.new_context()
                context.add_cookies([
                    {"name": name, "value": cookie.value, "domain": "testserver",
                     "path": cookie["path"] or "/", "secure": True}
                    for name, cookie in self.client.cookies.items()
                ])
                context.route("**/*", route_request)
                page = context.new_page()
                page.goto(self.origin + document_path + "#" + section, wait_until="domcontentloaded")
                self.assertEqual(page.locator('[data-vault-workspace].is-enhanced').count(), 1)
                card = page.locator(f"#add-{kind}-{section}")
                card.locator("summary").click()
                form = card.locator("form")
                self.assertEqual(form.get_attribute("action"), target_path)
                self.assertEqual(form.locator('[name="action"]').input_value(), "add_entry")
                form.locator('[name="body"]').fill(f"Browser {kind} content")
                if kind == "file":
                    form.locator('[name="file"]').set_input_files(
                        {"name": "browser.txt", "mimeType": "text/plain", "buffer": self.file_bytes},
                    )
                elif kind == "link":
                    form.locator('[name="url"]').fill("https://example.test/guide")
                with page.expect_navigation():
                    form.locator('button[type="submit"]').click()
                self.assertEqual(urlsplit(page.url).path, target_path)
                self.assertEqual(urlsplit(page.url).fragment, section)
            finally:
                browser.close()
        self.assertEqual(len(posted), 1)
        url, body, headers = posted[0]
        self.assertEqual(urlsplit(url).path, target_path)
        self.assertEqual(headers.get("origin"), self.origin)
        # Replay outside the browser event loop through Django's real CSRF middleware.
        extra = {"HTTP_COOKIE": headers.get("cookie", "")}
        extra.update({"HTTP_" + key.upper(): headers[key] for key in ("origin", "referer") if key in headers})
        result = self.client.post(urlsplit(url).path, data=body,
                                  content_type=headers["content-type"], secure=True, **extra)
        self.assertEqual(result.status_code, 302)

    def assert_all_forms(self, vault, document_path, response, *, staff):
        target = reverse("vault-staff-detail", args=[vault.pk]) if staff else document_path
        for kind in ("file", "link", "note"):
            section = "basics"
            with self.subTest(kind=kind):
                self.native_submit(document_path, response, target, kind, section)
                entry = vault.entries.get(kind=kind)
                self.assertEqual(entry.section, section)
                self.assertEqual(entry.body, f"Browser {kind} content")
                self.assertEqual(entry.author_type, "team" if staff else "client")
                if kind == "link":
                    self.assertEqual(entry.url, "https://example.test/guide")
                elif kind == "file":
                    self.assertNotIn(self.file_bytes, Path(entry.file.path).read_bytes())
                    download = reverse("vault-staff-file", args=[vault.pk, entry.pk]) if staff else reverse(
                        "vault-client-file", args=[vault.slug, entry.pk],
                    )
                    result = self.client.get(download, secure=True)
                    self.assertEqual(result.status_code, 200)
                    self.assertIn("attachment", result["Content-Disposition"])
                    self.assertEqual(b"".join(result.streaming_content), self.file_bytes)
        self.assertEqual(vault.entries.count(), 3)

    def test_staff_creation_response_posts_all_forms_to_detail_url(self):
        self.area_login(self.superadmin())
        path = reverse("vault-staff-list")
        org = Organization.objects.create(name="Synthetic browser Vault client")
        data = self.hidden_fields(self.client.get(path, secure=True), "form.vault-create-form")
        data["organization_id"] = str(org.pk)
        response = self.client.post(path, data, secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(response.status_code, 200)
        # The browser remains on the list URL while the rendered forms target detail.
        self.assert_all_forms(Vault.objects.get(organization=org), path, response, staff=True)

    def test_unlocked_client_posts_all_forms_with_active_section(self):
        path = reverse("vault-client", args=[self.vault.slug])
        data = self.hidden_fields(self.client.get(path, secure=True), "form.vault-form")
        data["code"] = self.code
        self.assertEqual(self.client.post(path, data, secure=True, HTTP_ORIGIN=self.origin).status_code, 302)
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assert_all_forms(self.vault, path, response, staff=False)
