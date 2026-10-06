"""Native HTTPS form submissions and dependent IndiaMART routing controls."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Pipeline, Stage
from apps.integrations.models import IndiaMartConnection
from apps.organizations.models import Organization


@override_settings(
    DEBUG=False,
    ALLOWED_HOSTS=["testserver"],
    SECURE_SSL_REDIRECT=True,
    CSRF_COOKIE_SECURE=True,
    SESSION_COOKIE_SECURE=True,
    CSRF_USE_SESSIONS=False,
    CSRF_TRUSTED_ORIGINS=[],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class IndiaMartSetupBrowserTests(TestCase):
    origin = "https://testserver"

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org = Organization.objects.create(name="IndiaMART browser seller")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(
            pipeline=self.pipeline, name="New enquiry", display_order=0
        )
        self.second_pipeline = Pipeline.objects.create(
            organization=self.org, name="Wholesale"
        )
        self.second_stage = Stage.objects.create(
            pipeline=self.second_pipeline, name="New wholesale enquiry", display_order=0
        )
        self.empty_pipeline = Pipeline.objects.create(
            organization=self.org, name="Without stages"
        )
        user = User.objects.create_superuser(
            email="indiamart-browser@example.test", name="Superadmin"
        )
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies[get_session_cookie_name("superadmin")] = session.session_key
        self.path = reverse(
            "superadmin-indiamart", kwargs={"organization_id": self.org.id}
        )

    def browser_submit(self, *, old_policy=False):
        response = self.client.get(self.path, secure=True)
        self.assertEqual(response.status_code, 200)
        headers = dict(response.items())
        if old_policy:
            headers["Referrer-Policy"] = "no-referrer"
        posted, errors = [], []
        script = (
            Path(settings.BASE_DIR) / "static/superadmin/indiamart-routing.js"
        ).read_text()

        def route_request(route):
            request = route.request
            if request.method == "POST" and request.url.startswith(self.origin + "/"):
                posted.append(
                    (request.url, request.post_data_buffer, request.all_headers())
                )
                route.fulfill(content_type="text/html", body="Submission captured")
            elif request.url == self.origin + self.path:
                route.fulfill(headers=headers, body=response.content)
            elif (
                urlsplit(request.url).path == "/static/superadmin/indiamart-routing.js"
            ):
                route.fulfill(content_type="application/javascript", body=script)
            else:
                route.abort()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("SHVYA_TEST_BROWSER"),
                args=["--no-sandbox"],
            )
            try:
                context = browser.new_context()
                context.add_cookies(
                    [
                        {
                            "name": name,
                            "value": cookie.value,
                            "url": self.origin + "/",
                            "secure": True,
                        }
                        for name, cookie in self.client.cookies.items()
                    ]
                )
                context.route("**/*", route_request)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(self.origin + self.path, wait_until="domcontentloaded")
                pipeline = page.get_by_label("Pipeline", exact=True)
                stage = page.get_by_label("Stage", exact=True)
                expect(stage).to_be_disabled()
                pipeline.select_option(str(self.pipeline.id))
                expect(stage).to_be_enabled()
                stage.select_option(str(self.stage.id))
                pipeline.select_option(str(self.second_pipeline.id))
                expect(stage).to_have_value("")
                self.assertEqual(
                    stage.locator('option:not([value=""])').evaluate_all(
                        "options => options.map(option => option.value)"
                    ),
                    [str(self.second_stage.id)],
                )
                pipeline.select_option(str(self.empty_pipeline.id))
                expect(stage).to_be_disabled()
                expect(page.locator("#id_stage_help")).to_contain_text(
                    "Add an active stage"
                )
                pipeline.select_option(str(self.second_pipeline.id))
                stage.select_option(str(self.second_stage.id))
                with page.expect_navigation():
                    page.get_by_role(
                        "button", name="Generate webhook URL", exact=True
                    ).click()
            finally:
                browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(posted), 1)
        url, body, submitted_headers = posted[0]
        extra = {"HTTP_COOKIE": submitted_headers.get("cookie", "")}
        for name in ("origin", "referer"):
            if name in submitted_headers:
                extra["HTTP_" + name.upper()] = submitted_headers[name]
        result = self.client.post(
            urlsplit(url).path,
            data=body,
            content_type=submitted_headers["content-type"],
            secure=True,
            **extra,
        )
        return result, submitted_headers

    def test_native_browser_generates_url_with_pipeline_and_stage_dropdowns(self):
        result, headers = self.browser_submit()
        self.assertEqual(headers.get("origin"), self.origin)
        self.assertEqual(headers.get("referer"), self.origin + self.path)
        self.assertEqual(result.status_code, 302)
        connection = IndiaMartConnection.objects.get(organization=self.org)
        self.assertTrue(connection.is_enabled)
        self.assertIsNotNone(connection.generated_at)
        self.assertEqual(connection.pipeline, self.second_pipeline)
        self.assertEqual(connection.stage, self.second_stage)

    def test_original_referrer_policy_reproduces_browser_csrf_failure(self):
        result, headers = self.browser_submit(old_policy=True)
        self.assertEqual(headers.get("origin"), "null")
        self.assertNotIn("referer", headers)
        self.assertEqual(result.status_code, 403)
        connection = IndiaMartConnection.objects.get(organization=self.org)
        self.assertFalse(connection.is_enabled)
        self.assertIsNone(connection.generated_at)
