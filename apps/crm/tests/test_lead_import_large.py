from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.crm.models import Lead, Pipeline
from apps.crm.tasks import import_leads_task
from apps.crm.views.lead_import import _review_rows
from apps.organizations.models import Organization
from services.crm.lead_import_service import get_import_job, save_import_state


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LargeLeadImportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Bulk Import Test")
        self.pipeline = Pipeline.objects.get(organization=self.organization, name="Leads")
        self.stage = self.pipeline.stages.get(name="Qualified")

    def test_review_uses_bounded_queries_for_10500_rows(self):
        rows = [
            {"Name": f"Lead {i}", "Phone": f"+9100000{i:05d}"}
            for i in range(1, 10501)
        ]
        with self.assertNumQueries(21):
            counts, preview = _review_rows(
                self.organization, rows, {"name": "Name", "phone": "Phone"},
            )
        self.assertEqual(counts["new_lead_count"], 10500)
        self.assertEqual(len(preview), 50)

    def test_import_skips_duplicate_and_does_not_send_welcome(self):
        token = "large-import-example"
        row = {"Name": "Demo Lead", "Phone": "+910000012345", "Email": "demo@example.com"}
        save_import_state(token, {
            "organization_id": str(self.organization.id),
            "pipeline_id": str(self.pipeline.id),
            "stage_id": str(self.stage.id),
            "mapping": {"name": "Name", "phone": "Phone", "email": "Email"},
            "rows": [row, row.copy()],
        })
        with patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome:
            import_leads_task.run(token, str(self.organization.id), "new_only")
        job = get_import_job(token)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["created_count"], 1)
        self.assertEqual(job["skipped_count"], 1)
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 1)
        welcome.assert_not_called()
