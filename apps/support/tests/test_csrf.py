"""Exercise support forms through the real session and CSRF middleware."""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from playwright.sync_api import sync_playwright

from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.organizations.models import Organization
from apps.support import services
from apps.support.models import Attachment, Ticket, TicketCategory, TicketIssue, TicketPriority, TicketStatus


@override_settings(
    DEBUG=False,
    ALLOWED_HOSTS=["testserver"],
    SECURE_SSL_REDIRECT=True,
    CSRF_COOKIE_SECURE=True,
    CSRF_USE_SESSIONS=False,
    CSRF_TRUSTED_ORIGINS=[],
    SUPPORT_PUBLIC_BASE_URL="https://testserver",
    SUPPORT_ATTACHMENT_SCANNER="",
    SUPPORT_REQUIRE_SCANNER=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class SupportCsrfTests(TestCase):
    origin = "https://testserver"

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="CSRF regression organization")
        cls.user = get_user_model().objects.create_user(
            email="csrf-requester@example.test", organization=cls.org,
            name="Requester", role="admin",
        )
        cls.staff = get_user_model().objects.create_superuser(
            email="csrf-ops@example.test", name="Support operator",
        )
        cls.category = TicketCategory.objects.create(name="CSRF regression category")
        cls.issue = TicketIssue.objects.create(category=cls.category, name="Ticket creation")
        cls.priority, _ = TicketPriority.objects.get_or_create(
            key="medium", defaults={"name": "Medium"},
        )
        for key in ("open", "in_progress", "answered", "on_hold", "closed"):
            TicketStatus.objects.get_or_create(
                key=key, defaults={"name": key, "behavior": key, "system": True},
            )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.login_area(self.user, "dashboard")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = FileSystemStorage(location=directory.name)
        for context in (
            patch.object(Attachment._meta.get_field("file"), "storage", storage),
            patch("apps.support.services.private_storage", storage),
            patch("apps.support.notifications.wake_delivery", lambda: None),
        ):
            context.start()
            self.addCleanup(context.stop)

    def login_area(self, user, area):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies[get_session_cookie_name(area)] = session.session_key

    def get_page(self, path):
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("noindex", response["X-Robots-Tag"])
        return response

    def hidden_fields(self, response, selector):
        form = BeautifulSoup(response.content, "html.parser").select_one(selector)
        self.assertIsNotNone(form)
        data = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"][name]')}
        self.assertTrue(data.get("csrfmiddlewaretoken"))
        return data

    def creation_data(self, response):
        data = self.hidden_fields(response, "form[data-issue-form]")
        data.update(category=str(self.category.pk), issue=str(self.issue.pk),
                    priority=str(self.priority.pk), subject="Cannot create ticket",
                    body="Steps to reproduce the support problem.")
        return data

    def create_ticket(self):
        return services.create_ticket(
            actor=self.user, category_id=self.category.pk, issue_id=self.issue.pk,
            priority_id=self.priority.pk, subject="CSRF test ticket", body="Initial description",
        )

    def test_https_creation_persists_ticket_and_attachment(self):
        response = self.get_page(reverse("support-client:list"))
        data = self.creation_data(response)
        data["attachments"] = SimpleUploadedFile("steps.txt", b"Reproduction steps", "text/plain")
        result = self.client.post(reverse("support-client:create"), data,
                                  secure=True, HTTP_ORIGIN=self.origin)
        ticket = Ticket.objects.get()
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.url, reverse("support-client:detail", args=[ticket.pk]))
        self.assertEqual(ticket.organization, self.org)
        self.assertEqual(ticket.requester, self.user)
        self.assertEqual(ticket.messages.get().body, data["body"])
        self.assertEqual(Attachment.objects.get(message__ticket=ticket).original_name, "steps.txt")

    def test_https_referer_fallback_accepts_same_origin(self):
        path = reverse("support-client:list")
        data = self.creation_data(self.get_page(path))
        result = self.client.post(reverse("support-client:create"), data,
                                  secure=True, HTTP_REFERER=self.origin + path)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_missing_invalid_and_foreign_csrf_requests_remain_blocked(self):
        data = self.creation_data(self.get_page(reverse("support-client:list")))
        cases = (
            ({**data, "csrfmiddlewaretoken": ""}, {"HTTP_ORIGIN": self.origin}),
            ({**data, "csrfmiddlewaretoken": "x" * 64}, {"HTTP_ORIGIN": self.origin}),
            (data, {"HTTP_ORIGIN": "null"}),
            (data, {"HTTP_ORIGIN": "https://untrusted.example.test"}),
            (data, {}),
        )
        for payload, headers in cases:
            with self.subTest(headers=headers, token=bool(payload["csrfmiddlewaretoken"])):
                result = self.client.post(reverse("support-client:create"), payload, secure=True, **headers)
                self.assertEqual(result.status_code, 403)
        del self.client.cookies[settings.CSRF_COOKIE_NAME]
        result = self.client.post(reverse("support-client:create"), data,
                                  secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(result.status_code, 403)
        self.assertFalse(Ticket.objects.exists())

    def test_invalid_form_keeps_data_and_can_be_resubmitted(self):
        data = self.creation_data(self.get_page(reverse("support-client:list")))
        data["subject"] = ""
        response = self.client.post(reverse("support-client:create"), data,
                                    secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertContains(response, data["body"], status_code=400)
        data.update(self.hidden_fields(response, "form[data-issue-form]"))
        data["subject"] = "Corrected subject"
        result = self.client.post(reverse("support-client:create"), data,
                                  secure=True, HTTP_ORIGIN=self.origin)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(Ticket.objects.get().subject, "Corrected subject")

    def test_customer_and_staff_replies_work_with_csrf(self):
        ticket = self.create_ticket()
        for user, area, namespace in (
            (self.user, "dashboard", "support-client"),
            (self.staff, "superadmin", "support-staff"),
        ):
            with self.subTest(area=area):
                self.login_area(user, area)
                response = self.get_page(reverse(f"{namespace}:detail", args=[ticket.pk]))
                data = self.hidden_fields(response, "form[data-reply-form]")
                data["body"] = f"Reply from {area}"
                result = self.client.post(reverse(f"{namespace}:reply", args=[ticket.pk]), data,
                                          secure=True, HTTP_ORIGIN=self.origin)
                self.assertEqual(result.status_code, 302)
                self.assertTrue(ticket.messages.filter(body=data["body"], author=user).exists())

    def test_shared_reply_keeps_csrf_and_privacy(self):
        ticket = self.create_ticket()
        _, token = services.issue_share(actor=self.user, ticket_id=ticket.pk, label="Viewer", can_reply=True)
        self.client = Client(enforce_csrf_checks=True)
        response = self.get_page(reverse("support-shared:ticket", args=[token]))
        self.assertContains(response, '<meta name="referrer" content="same-origin">')
        data = self.hidden_fields(response, "form[data-reply-form]")
        data["body"] = "Shared viewer reply"
        path = reverse("support-shared:reply", args=[token])
        self.assertEqual(self.client.post(path, data, secure=True,
                                         HTTP_ORIGIN="https://untrusted.example.test").status_code, 403)
        self.assertEqual(self.client.post(path, data, secure=True, HTTP_ORIGIN=self.origin).status_code, 302)
        self.assertTrue(ticket.messages.filter(body=data["body"]).exists())

    def browser_submit(self, path, *, shared=False, old_policy=False):
        """Capture a native Chromium POST, then replay its exact body/headers to Django.

        Browser traffic is fulfilled in memory, including the actual support JS.
        Database work occurs outside Playwright's event loop. No live site or
        external email/AI provider is contacted, and no CSRF headers are fabricated.
        """
        response = self.get_page(path)
        headers = dict(response.items())
        if old_policy:
            headers["Referrer-Policy"] = "no-referrer"
        posted = []
        script = (Path(settings.BASE_DIR) / "static/support/support.js").read_text()

        def route_request(route):
            request = route.request
            if request.method == "POST" and request.url.startswith(self.origin + "/"):
                posted.append((request.url, request.post_data_buffer, request.all_headers()))
                route.fulfill(status=200, content_type="text/html", body="Submission captured")
            elif request.url == self.origin + path:
                route.fulfill(status=200, headers=headers, body=response.content)
            elif urlsplit(request.url).path == "/static/support/support.js":
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
                    {"name": name, "value": cookie.value, "url": self.origin + "/", "secure": True}
                    for name, cookie in self.client.cookies.items()
                ])
                context.route("**/*", route_request)
                page = context.new_page()
                page.goto(self.origin + path, wait_until="domcontentloaded")
                if shared:
                    form = page.locator("form[data-reply-form]")
                    form.locator('[name="body"]').fill("Native browser shared reply")
                else:
                    page.locator('[data-open-dialog="new-ticket"]').first.click()
                    form = page.locator("form[data-issue-form]")
                    form.locator('[name="category"]').select_option(str(self.category.pk))
                    form.locator('[name="issue"]').select_option(str(self.issue.pk))
                    form.locator('[name="priority"]').select_option(str(self.priority.pk))
                    form.locator('[name="subject"]').fill("Native browser ticket")
                    form.locator('[name="body"]').fill("Native multipart form submission")
                with page.expect_navigation():
                    form.locator('button[type="submit"]').click()
            finally:
                browser.close()
        self.assertEqual(len(posted), 1)
        url, body, submitted_headers = posted[0]
        extra = {"HTTP_COOKIE": submitted_headers.get("cookie", "")}
        for name in ("origin", "referer"):
            if name in submitted_headers:
                extra["HTTP_" + name.upper()] = submitted_headers[name]
        result = self.client.post(urlsplit(url).path, data=body,
                                  content_type=submitted_headers["content-type"], secure=True, **extra)
        return result, submitted_headers

    def test_native_browser_ticket_submission(self):
        result, headers = self.browser_submit(reverse("support-client:list"))
        self.assertEqual(headers.get("origin"), self.origin)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(Ticket.objects.get().subject, "Native browser ticket")

    def test_old_privacy_header_reproduces_browser_403(self):
        result, headers = self.browser_submit(reverse("support-client:list"), old_policy=True)
        self.assertEqual(headers.get("origin"), "null")
        self.assertNotIn("referer", headers)
        self.assertEqual(result.status_code, 403)
        self.assertFalse(Ticket.objects.exists())

    def test_native_browser_shared_reply(self):
        ticket = self.create_ticket()
        _, token = services.issue_share(actor=self.user, ticket_id=ticket.pk, label="Viewer", can_reply=True)
        self.client = Client(enforce_csrf_checks=True)
        result, headers = self.browser_submit(reverse("support-shared:ticket", args=[token]), shared=True)
        self.assertEqual(headers.get("origin"), self.origin)
        self.assertEqual(result.status_code, 302)
        self.assertTrue(ticket.messages.filter(body="Native browser shared reply").exists())
