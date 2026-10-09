"""Regression checks for Sales Templates authentication."""
from django.test import RequestFactory, SimpleTestCase
from unittest.mock import patch

from apps.sales.views_templates import sales_template_list_view


class SalesTemplateListAuthTests(SimpleTestCase):
    def test_unauthenticated_request_redirects_before_template_access(self):
        request = RequestFactory().get("/dashboard/shvya-sales/templates/")
        with patch("apps.sales.views_templates.ensure_default_templates") as ensure:
            response = sales_template_list_view(request)
        self.assertIn(response.status_code, (302, 401, 403))
        ensure.assert_not_called()
