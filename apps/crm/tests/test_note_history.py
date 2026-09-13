from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.template.loader import render_to_string

from apps.crm.models import Lead, LeadNote, Pipeline
from apps.crm.views.dashboard import lead_note_save
from apps.organizations.models import Organization


class NoteHistoryTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Note history")
        self.user = get_user_model().objects.create(
            email="note-history@example.com", organization=self.org, role="admin",
        )
        self.pipeline = Pipeline.objects.get(organization=self.org, name="Leads")
        self.lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline,
            stage=self.pipeline.stages.first(), name="Notes",
            phone="+919999999992", notes="Imported context",
        )
        self.factory = RequestFactory()

    def save_note(self, text):
        request = self.factory.post("/", {"note": text})
        request.crm_user = self.user
        # Exercise the HTTP view and tenant lookup with an already authenticated user.
        return lead_note_save.__wrapped__(request, self.lead.pk)

    def test_repeated_add_preserves_legacy_and_all_manual_notes(self):
        for text in ("First note", "Second note", "Third note"):
            self.assertEqual(self.save_note(text).status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.notes, "Imported context")
        self.assertEqual(LeadNote.objects.filter(lead=self.lead).count(), 3)
        html = render_to_string("crm/partials/notes_editor.html", {"note_lead": self.lead})
        for text in ("Imported context", "First note", "Second note", "Third note"):
            self.assertIn(text, html)

    def test_empty_note_does_not_remove_history(self):
        self.save_note("Keep this")
        self.assertEqual(self.save_note("  ").status_code, 400)
        self.assertEqual(self.lead.lead_notes.get().note, "Keep this")

    def test_other_organization_cannot_add_note(self):
        self.user.organization = Organization.objects.create(name="Other tenant")
        with self.assertRaises(Http404):
            self.save_note("Not allowed")
        self.assertFalse(self.lead.lead_notes.exists())
