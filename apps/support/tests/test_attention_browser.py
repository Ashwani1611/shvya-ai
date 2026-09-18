"""Isolated Chromium checks for the real indicator assets, not production data."""
import json
import shutil
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "http://127.0.0.1:8765"
PORTAL = "/dashboard/support-portal/"
ENDPOINT = PORTAL + "attention/"


class SupportAttentionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            executable_path=shutil.which("chromium") or shutil.which("google-chrome"),
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.count = 1
        self.response_status = 200
        self.requests = []
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.route)

    def html(self):
        config = json.dumps({"count": self.count, "endpoint": ENDPOINT, "portal": PORTAL})
        return '''<!doctype html><html><head>
        <link rel="stylesheet" href="/static/support/attention.css">
        <script src="/static/support/attention.js" defer></script></head><body>
        <aside id="app-sidebar"><a class="shvya-nav-row" href="''' + PORTAL + '''" title="Help & Support">
        <i class="ti ti-headset"></i><span class="shvya-nav-copy">Help &amp; Support</span></a></aside>
        <script type="application/json" id="shvya-support-attention">''' + config + '''</script>
        </body></html>'''

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path == ENDPOINT:
            self.requests.append(route.request.method)
            route.fulfill(status=self.response_status, content_type="application/json", body=json.dumps({"count": self.count}))
        elif path.startswith("/static/support/"):
            asset = ROOT / path.lstrip("/")
            route.fulfill(content_type="text/css" if path.endswith(".css") else "application/javascript", body=asset.read_text())
        elif path in (PORTAL, "/dashboard/"):
            route.fulfill(content_type="text/html", body=self.html())
        else:
            route.abort()

    def load(self):
        self.page.goto(ORIGIN + "/dashboard/", wait_until="networkidle")

    def refresh(self):
        old_count = len(self.requests)
        with self.page.expect_response(ORIGIN + ENDPOINT):
            self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.page.wait_for_timeout(50)
        self.assertGreater(len(self.requests), old_count)

    def test_initial_blue_pulse_and_count_keep_existing_navigation(self):
        self.load()
        row = self.page.locator("#app-sidebar a")
        self.assertIn("support-needs-response", row.get_attribute("class"))
        self.assertEqual(row.locator(".support-attention-badge").inner_text(), "1")
        self.assertIn("Reply or close", row.get_attribute("aria-label"))
        self.assertEqual(row.evaluate("n => getComputedStyle(n).animationIterationCount"), "infinite")
        self.assertEqual(row.evaluate("n => getComputedStyle(n).color"), "rgb(0, 102, 204)")
        self.assertEqual(row.get_attribute("href"), PORTAL)
        self.assertFalse(self.errors)

    def test_click_view_and_reload_do_not_acknowledge(self):
        self.load()
        self.page.locator("#app-sidebar a").click()
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.page.url, ORIGIN + PORTAL)
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)
        self.page.reload(wait_until="networkidle")
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)
        self.assertFalse(any(method != "GET" for method in self.requests))

    def test_updates_live_clears_only_at_zero_and_reactivates(self):
        self.count = 2
        self.load()
        self.count = 1
        self.refresh()
        self.assertEqual(self.page.locator(".support-attention-badge").inner_text(), "1")
        self.count = 0
        self.refresh()
        self.assertEqual(self.page.locator(".support-needs-response").count(), 0)
        self.assertFalse(self.page.locator(".support-attention-badge").is_visible())
        self.count = 3
        self.refresh()
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)
        self.assertEqual(self.page.locator(".support-attention-badge").inner_text(), "3")

    def test_server_errors_are_not_acknowledgements(self):
        self.load()
        self.response_status = 503
        self.count = 0
        self.refresh()
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)
        self.assertEqual(self.page.locator(".support-attention-badge").inner_text(), "1")
        self.assertFalse(self.errors)

    def test_reduced_motion_keeps_a_static_blue_attention_marker(self):
        self.context.close()
        self.context = self.browser.new_context(reduced_motion="reduce")
        self.addCleanup(self.context.close)
        self.context.route("**/*", self.route)
        self.page = self.context.new_page()
        self.load()
        row = self.page.locator(".support-needs-response")
        self.assertEqual(row.evaluate("n => getComputedStyle(n).animationName"), "none")
        self.assertEqual(row.evaluate("n => getComputedStyle(n).color"), "rgb(0, 102, 204)")
        self.assertTrue(row.locator(".support-attention-badge").is_visible())

    def test_collapsed_sidebar_keeps_visible_blue_dot(self):
        self.load()
        self.page.locator("#app-sidebar").evaluate("n => n.classList.add('sidebar-collapsed')")
        badge = self.page.locator(".support-attention-badge")
        self.assertEqual(badge.evaluate("n => getComputedStyle(n).width"), "8px")
        self.assertTrue(badge.is_visible())
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)

    def test_sidebar_rebuild_and_duplicate_asset_never_duplicate_badges(self):
        self.load()
        self.page.locator("#app-sidebar").evaluate("n => n.replaceChildren(n.firstElementChild.cloneNode(true))")
        self.page.add_script_tag(path=str(ROOT / "static/support/attention.js"))
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator(".support-attention-badge").count(), 1)
        self.assertEqual(self.page.locator(".support-attention-sr").count(), 1)
        self.assertEqual(self.page.locator(".support-needs-response").count(), 1)
        self.assertFalse(self.errors)

    def test_mobile_badge_and_large_count_do_not_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.count = 120
        self.load()
        self.assertEqual(self.page.locator(".support-attention-badge").inner_text(), "99+")
        self.assertIn("120 tickets", self.page.locator("#app-sidebar a").get_attribute("aria-label"))
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

    def test_auth_revocation_stops_polling_and_removes_stale_tenant_indicator(self):
        self.load()
        self.response_status = 403
        self.refresh()
        self.assertEqual(self.page.locator(".support-needs-response").count(), 0)
        requests = len(self.requests)
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.requests), requests)


if __name__ == "__main__":
    unittest.main()
