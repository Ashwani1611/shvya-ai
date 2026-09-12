from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from services.crm_activity_service import record_lead_created


class LeadCreatedActivitySourceTests(SimpleTestCase):
    def _lead(self, source):
        return SimpleNamespace(
            organization=object(),
            pipeline=SimpleNamespace(name="Sales"),
            stage=SimpleNamespace(name="New Lead"),
            name="Sheet Lead",
            email="",
            phone="+919876543210",
            lead_source=source,
        )

    @patch("services.crm_activity_service.LeadActivity.objects.create")
    def test_google_sheet_creation_is_attributed_to_google_sheet(self, create):
        record_lead_created(
            lead=self._lead("google_sheets"),
            actor=None,
        )

        payload = create.call_args.kwargs
        self.assertEqual(payload["actor_name"], "Google Sheet")
        self.assertEqual(payload["details"]["lead_source"], "google_sheets")

    @patch("services.crm_activity_service.LeadActivity.objects.create")
    def test_normal_system_creation_keeps_system_fallback(self, create):
        record_lead_created(
            lead=self._lead("system"),
            actor=None,
        )

        payload = create.call_args.kwargs
        self.assertEqual(payload["actor_name"], "")


    @patch("services.crm_activity_service.LeadActivity.objects.create")
    def test_meta_ads_creation_is_attributed_to_meta_ads(self, create):
        record_lead_created(
            lead=self._lead("meta_ads"),
            actor=None,
        )

        payload = create.call_args.kwargs
        self.assertEqual(payload["actor_name"], "Meta ads")
        self.assertEqual(payload["details"]["lead_source"], "meta_ads")
