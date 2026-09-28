from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.crm.models import Lead, LeadActivity, Pipeline
from apps.crm.tasks import import_leads_task
from apps.crm.views.lead_import import _review_rows
from apps.organizations.models import Organization
from services.crm.lead_import_service import get_import_job, save_import_state


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LargeLeadImportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = Organization.objects.create(name="Bulk Import Test")
        self.pipeline = Pipeline.objects.get(
            organization=self.organization,
            name="Leads",
        )
        self.stage = self.pipeline.stages.get(name="Qualified")

    def _state(self, rows):
        return {
            "organization_id": str(self.organization.id),
            "pipeline_id": str(self.pipeline.id),
            "stage_id": str(self.stage.id),
            "mapping": {
                "name": "Name",
                "phone": "Phone",
                "email": "Email",
            },
            "rows": rows,
        }

    def test_review_uses_bounded_queries_for_10500_rows(self):
        rows = [
            {"Name": f"Lead {i}", "Phone": f"+9100000{i:05d}"}
            for i in range(1, 10501)
        ]
        with self.assertNumQueries(21):
            counts, preview = _review_rows(
                self.organization,
                rows,
                {"name": "Name", "phone": "Phone"},
            )
        self.assertEqual(counts["new_lead_count"], 10500)
        self.assertEqual(len(preview), 50)

    def test_import_is_isolated_to_ingestion_queue(self):
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["crm.import_leads"]["queue"],
            "ingestion",
        )

    def test_import_skips_duplicate_and_does_not_send_welcome(self):
        token = "large-import-example"
        row = {
            "Name": "Demo Lead",
            "Phone": "+910000012345",
            "Email": "demo@example.com",
        }
        save_import_state(token, self._state([row, row.copy()]))

        with patch(
            "services.crm.lead_service._schedule_new_lead_welcome"
        ) as welcome:
            import_leads_task.run(
                token,
                str(self.organization.id),
                "new_only",
            )

        job = get_import_job(token)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["created_count"], 1)
        self.assertEqual(job["skipped_count"], 1)
        self.assertEqual(
            Lead.objects.filter(organization=self.organization).count(),
            1,
        )
        self.assertEqual(
            LeadActivity.objects.filter(
                organization=self.organization,
                topic=LeadActivity.Topic.LEAD_CREATED,
            ).count(),
            1,
        )
        welcome.assert_not_called()

    def test_1000_row_import_has_bounded_database_work_and_no_workflow_fanout(self):
        token = "bounded-import-example"
        rows = [
            {
                "Name": f"Lead {i}",
                "Phone": f"+9199900{i:05d}",
                "Email": "",
            }
            for i in range(1, 1001)
        ]
        save_import_state(token, self._state(rows))

        with patch("apps.triggers.signals.emit") as emit:
            with CaptureQueriesContext(connection) as queries:
                import_leads_task.run(
                    token,
                    str(self.organization.id),
                    "new_only",
                )

        self.assertLess(
            len(queries.captured_queries),
            80,
            "Large imports regressed to per-lead database queries.",
        )
        self.assertEqual(
            Lead.objects.filter(organization=self.organization).count(),
            1000,
        )
        self.assertEqual(
            LeadActivity.objects.filter(
                organization=self.organization,
                topic=LeadActivity.Topic.LEAD_CREATED,
            ).count(),
            1000,
        )
        emit.assert_not_called()
