import uuid
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone


class LeadSourceDisplayTests(SimpleTestCase):
    def test_google_sheet_source_is_visible_in_lead_details(self):
        now = timezone.now()
        lead = SimpleNamespace(
            id=uuid.uuid4(),
            name="Sheet Lead",
            phone="",
            email="",
            pipeline=SimpleNamespace(name="Sales"),
            stage=SimpleNamespace(name="New Lead"),
            created_at=now,
            updated_at=now,
            attributes={},
            get_lead_source_display=lambda: "Google Sheet",
        )

        html = render_to_string(
            "crm/partials/lead_detail.html",
            {
                "lead": lead,
                "initials": "SL",
                "lead_note_text": "",
                "calls": [],
                "reminders": [],
                "contacts": [],
            },
        )

        self.assertIn("Source", html)
        self.assertIn("Google Sheet", html)
