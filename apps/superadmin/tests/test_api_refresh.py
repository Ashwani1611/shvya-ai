"""Refresh regressions run in the existing Superadmin CI test target."""
from pathlib import Path
import shutil
import subprocess
from unittest import TestCase as UnitTestCase

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.superadmin.tests import test_api_operations as fixtures


class APIRefreshJavascriptTests(UnitTestCase):
    def test_refresh_state_machine(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for API refresh regression tests.")
        script = Path(__file__).parent / "js" / "api-operations.test.cjs"
        result = subprocess.run(
            [node, "--test", str(script)], check=False, capture_output=True,
            text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@override_settings(API_ACTIVITY_ENABLED=False, CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "api-refresh-render-tests",
}})
class APIRefreshRenderTests(TestCase):
    def setUp(self):
        fixtures.APIActivityWorkspaceTests.setUp(self)

    def test_dashboard_exposes_one_accessible_opt_in_control(self):
        response = self.client.get(reverse("superadmin-api"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="api-auto-refresh"', count=1)
        self.assertContains(response, 'aria-describedby="api-auto-refresh-status"')
        self.assertContains(response, 'data-api-refresh-controls hidden')
        self.assertContains(response, 'data-api-refresh-status')
        self.assertContains(response, 'api-refresh.css')
        self.assertContains(response, 'Auto-refresh is off.')
