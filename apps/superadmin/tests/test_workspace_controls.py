from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.organizations.models import Organization, OrganizationTag


class SuperadminWorkspaceControlsTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            email="superadmin-workspace@example.com",
            password="test-password-123",
            name="Super Admin",
        )
        session = SessionStore()
        set_authenticated_user(session, self.superuser)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

        self.organization = Organization.objects.create(name="Premium Workspace Org")
        self.user = User.objects.create_user(
            email="agent-workspace@example.com",
            organization=self.organization,
            password="old-password-123",
            name="Workspace Agent",
        )

    def test_operational_notes_are_saved_with_update_timestamp(self):
        url = reverse(
            "superadmin-organization-notes-update",
            kwargs={"organization_id": self.organization.id},
        )

        response = self.client.post(
            url,
            {"operational_notes": "Customer requested a rollout review on Friday."},
        )

        self.assertEqual(response.status_code, 302)
        self.organization.refresh_from_db()
        self.assertEqual(
            self.organization.operational_notes,
            "Customer requested a rollout review on Friday.",
        )
        self.assertTrue(
            self.organization.settings.get("operational_notes_updated_at")
        )

    def test_tag_editor_reuses_existing_tags_and_creates_new_ones(self):
        trial = OrganizationTag.objects.create(name="Trial")
        url = reverse(
            "superadmin-organization-tags-update",
            kwargs={"organization_id": self.organization.id},
        )

        response = self.client.post(url, {"tags": "trial, Growth Lab, Trial"})

        self.assertEqual(response.status_code, 302)
        self.organization.refresh_from_db()
        names = set(self.organization.tags.values_list("name", flat=True))
        self.assertEqual(names, {"Trial", "Growth Lab"})
        self.assertEqual(OrganizationTag.objects.filter(pk=trial.pk).count(), 1)

    def test_password_reset_changes_selected_organization_user_password(self):
        url = reverse(
            "superadmin-organization-user-reset-password",
            kwargs={"organization_id": self.organization.id},
        )

        response = self.client.post(
            url,
            {
                "user_id": str(self.user.id),
                "new_password1": "New-secure-password-456!",
                "new_password2": "New-secure-password-456!",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-secure-password-456!"))

    def test_password_reset_validation_redirects_back_to_open_modal(self):
        url = reverse(
            "superadmin-organization-user-reset-password",
            kwargs={"organization_id": self.organization.id},
        )

        response = self.client.post(
            url,
            {
                "user_id": str(self.user.id),
                "new_password1": "different-password-123!",
                "new_password2": "different-password-456!",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("reset_password=1", response["Location"])
        self.assertIn(str(self.user.id), response["Location"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-password-123"))
