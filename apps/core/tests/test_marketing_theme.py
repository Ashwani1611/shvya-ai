"""Public appearance must cover every marketing route without changing CRM."""
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve


class MarketingThemeTests(SimpleTestCase):
    paths = (
        "/", "/features/", "/pricing/", "/services/", "/product-suite/",
        "/docs/", "/book-a-call/", "/privacy-policy/", "/terms-conditions/",
        "/refund-policy/", "/cookie-policy/",
    )

    def render_page(self, path, user=None):
        request = RequestFactory().get(path)
        request.session = {}
        with patch("apps.core.views.get_crm_authenticated_user", return_value=user):
            response = resolve(path).func(request)
            response.render()
        self.assertEqual(response.status_code, 200)
        return response

    def test_every_public_page_uses_one_theme_control_and_shared_assets(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.render_page(path)
                self.assertContains(response, 'class="marketing-site"', count=1)
                self.assertContains(response, "data-marketing-theme-toggle", count=1)
                self.assertContains(response, 'id="marketing-day-theme"', count=1)
                self.assertContains(response, "marketing/dark/site.css")
                self.assertContains(response, "marketing/day.css")
                self.assertContains(response, "marketing/theme.js")
                self.assertContains(response, 'media="(prefers-color-scheme: light)"')

    def test_initialization_runs_in_head_and_day_palette_follows_feature_styles(self):
        html = self.render_page("/features/").content.decode()
        self.assertLess(html.index("marketing/premium-features.css"), html.index("marketing/day.css"))
        self.assertLess(html.index("marketing/theme.js"), html.index("</head>"))
        script_tag = html[html.rfind("<script", 0, html.index("marketing/theme.js")):]
        script_tag = script_tag.split(">", 1)[0]
        self.assertNotIn("defer", script_tag)
        self.assertNotIn("async", script_tag)
        self.assertIn("marketing/premium-features.js", html)

    def test_signed_in_navigation_is_retained_with_the_theme_control(self):
        response = self.render_page("/features/", {"name": "Demo User"})
        self.assertContains(response, "Demo User")
        self.assertContains(response, "data-marketing-theme-toggle", count=1)
        self.assertContains(response, ">Profile</a>", count=2)
        self.assertNotContains(response, 'class="nav-login"')

    def test_theme_behavior_with_dependency_free_node_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable; run the standalone JS suite in a Node environment")
        result = subprocess.run(
            [node, "--test", "tests/js/marketing-theme.test.cjs"],
            cwd=Path(settings.BASE_DIR), capture_output=True, text=True, timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
