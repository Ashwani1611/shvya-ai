from django.test import SimpleTestCase


class ViewImportsTest(SimpleTestCase):
    def test_all_views_importable(self):
        from apps.crm.views import (
            crm_login_view,
            dashboard_view,
        )
        self.assertTrue(callable(crm_login_view))
        self.assertTrue(callable(dashboard_view))
