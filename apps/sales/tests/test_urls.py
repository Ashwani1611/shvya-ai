import uuid

from django.test import SimpleTestCase
from django.urls import reverse


class SalesUrlTests(SimpleTestCase):
    def test_dashboard_routes_under_dashboard_sales(self):
        self.assertEqual(reverse("shvya-sales-dashboard"), "/dashboard/sales/")
        self.assertEqual(
            reverse("shvya-sales-document-list"),
            "/dashboard/sales/documents/",
        )

    def test_public_document_route_is_outside_authenticated_dashboard(self):
        token = uuid.UUID("4bc6a9fe-3dd8-4ccc-91ba-f3134f10d15a")
        self.assertEqual(
            reverse("shvya-sales-public-document", args=[token]),
            f"/sales/{token}/",
        )
