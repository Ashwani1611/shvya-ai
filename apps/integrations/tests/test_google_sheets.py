import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.integrations.models import GoogleSheetIntegration
from apps.integrations.services.google_sheets import (
    build_google_apps_script,
    process_google_sheet_rows,
)
from apps.organizations.models import Organization


class GoogleSheetsIntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Sheets Test Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=0,
        )
        self.attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            name="Budget",
            key="budget",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.integration = GoogleSheetIntegration.objects.create(
            organization=self.organization,
            name="Meta Leads",
            worksheet_name="Leads",
            pipeline=self.pipeline,
            stage=self.stage,
            mapping={
                "Full Name": "core:name",
                "Mobile": "core:phone",
                "Email": "core:email",
                "Budget": f"attribute:{self.attribute.id}",
            },
            discovered_headers=["Full Name", "Mobile", "Email", "Budget"],
            is_enabled=True,
        )
        self.integration.set_secret("sheet-secret")
        self.integration.save(update_fields=["encrypted_secret"])

    def test_generated_apps_script_uses_installable_triggers_without_oauth2_library(self):
        script = build_google_apps_script(
            integration=self.integration,
            webhook_url=(
                "https://dashboard.shvya-ai.com/dashboard/connect-hub/"
                "google-sheets/webhook/token/"
            ),
        )
        self.assertIn("ScriptApp.newTrigger", script)
        self.assertIn("everyMinutes(5)", script)
        self.assertIn("X-Shvya-Sheets-Secret", script)
        self.assertNotIn("OAuth2.createService", script)
        self.assertIn('"sheetName":"Leads"', script)

    def test_register_event_discovers_headers(self):
        url = reverse(
            "google-sheets-ingest",
            kwargs={"token": self.integration.webhook_token},
        )
        response = self.client.post(
            url,
            data=json.dumps(
                {
                    "event": "register",
                    "spreadsheet_id": "spreadsheet-123",
                    "spreadsheet_url": (
                        "https://docs.google.com/spreadsheets/d/spreadsheet-123/edit"
                    ),
                    "sheet_id": 9988,
                    "sheet_name": "Leads",
                    "headers": ["Full Name", "Mobile", "Budget", "Mobile", ""],
                }
            ),
            content_type="application/json",
            HTTP_X_SHVYA_SHEETS_SECRET="sheet-secret",
        )
        self.assertEqual(response.status_code, 200)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.spreadsheet_id, "spreadsheet-123")
        self.assertEqual(
            self.integration.discovered_headers,
            ["Full Name", "Mobile", "Budget"],
        )
        self.assertIsNotNone(self.integration.last_registered_at)

    @patch("apps.integrations.views.google_sheets.process_google_sheet_rows_task.delay")
    def test_rows_are_queued_for_async_processing(self, delay):
        url = reverse(
            "google-sheets-ingest",
            kwargs={"token": self.integration.webhook_token},
        )
        rows = [{"row_number": 2, "values": {"Mobile": "919876543210"}}]
        response = self.client.post(
            url,
            data=json.dumps(
                {
                    "event": "rows",
                    "sheet_name": "Leads",
                    "rows": rows,
                }
            ),
            content_type="application/json",
            HTTP_X_SHVYA_SHEETS_SECRET="sheet-secret",
        )
        self.assertEqual(response.status_code, 202)
        delay.assert_called_once_with(str(self.integration.id), rows)

    def test_wrong_secret_is_rejected(self):
        url = reverse(
            "google-sheets-ingest",
            kwargs={"token": self.integration.webhook_token},
        )
        response = self.client.post(
            url,
            data=json.dumps({"event": "ping"}),
            content_type="application/json",
            HTTP_X_SHVYA_SHEETS_SECRET="wrong-secret",
        )
        self.assertEqual(response.status_code, 403)

    def test_process_rows_creates_then_updates_lead_and_attributes(self):
        result = process_google_sheet_rows(
            integration_id=str(self.integration.id),
            rows=[
                {
                    "row_number": 2,
                    "values": {
                        "Full Name": "Rahul Sharma",
                        "Mobile": "91 98765 43210",
                        "Email": "rahul@example.com",
                        "Budget": "50000",
                    },
                }
            ],
        )
        self.assertEqual(result["created"], 1)
        lead = Lead.objects.get(
            organization=self.organization,
            phone="+919876543210",
        )
        self.assertEqual(lead.name, "Rahul Sharma")
        self.assertEqual(lead.lead_source, "google_sheets")
        self.assertEqual(lead.attributes["budget"], "50000")
        self.assertEqual(lead.pipeline, self.pipeline)
        self.assertEqual(lead.stage, self.stage)

        result = process_google_sheet_rows(
            integration_id=str(self.integration.id),
            rows=[
                {
                    "row_number": 2,
                    "values": {
                        "Full Name": "Rahul S.",
                        "Mobile": "+91-9876543210",
                        "Email": "rahul@example.com",
                        "Budget": "75000",
                    },
                }
            ],
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            Lead.objects.filter(organization=self.organization).count(),
            1,
        )
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Rahul S.")
        self.assertEqual(lead.attributes["budget"], "75000")

    def test_rows_without_phone_are_skipped_without_aborting_batch(self):
        result = process_google_sheet_rows(
            integration_id=str(self.integration.id),
            rows=[
                {"row_number": 2, "values": {"Full Name": "No Phone"}},
                {
                    "row_number": 3,
                    "values": {
                        "Full Name": "Good",
                        "Mobile": "919876543211",
                    },
                },
            ],
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 1)
        self.assertTrue(Lead.objects.filter(phone="+919876543211").exists())
