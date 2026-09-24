from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.organizations.models import Organization


class SuperadminAccountInformationAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="primary-superadmin@example.com",
            password="StrongAdminPassword123!",
            name="Primary Superadmin",
        )
        self.client.force_login(self.admin)

    def test_account_information_lists_existing_and_legacy_superadmins_only(self):
        legacy = User.objects.create_user(
            email="legacy-superadmin@example.com",
            organization=None,
            password="LegacyPassword123!",
            name="Legacy Superadmin",
            role=User.Role.SUPERADMIN,
        )
        organization = Organization.objects.create(name="Customer Workspace")
        normal_user = User.objects.create_user(
            email="customer-admin@example.com",
            organization=organization,
            password="CustomerPassword123!",
            name="Customer Admin",
            role=User.Role.ADMIN,
        )

        response = self.client.get(
            reverse("admin:accounts_superadminaccount_changelist")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.admin.email)
        self.assertContains(response, legacy.email)
        self.assertNotContains(response, normal_user.email)

    def test_create_superadmin_sets_required_platform_flags(self):
        response = self.client.post(
            reverse("admin:accounts_superadminaccount_add"),
            {
                "email": "new-superadmin@example.com",
                "name": "New Superadmin",
                "phone": "+911234567890",
                "is_active": "on",
                "password1": "StrongNewPassword123!",
                "password2": "StrongNewPassword123!",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        created = User.objects.get(email="new-superadmin@example.com")
        self.assertIsNone(created.organization_id)
        self.assertEqual(created.role, User.Role.SUPERADMIN)
        self.assertTrue(created.is_staff)
        self.assertTrue(created.is_superuser)
        self.assertTrue(created.is_active)
        self.assertTrue(created.check_password("StrongNewPassword123!"))

    def test_editing_legacy_superadmin_repairs_access_flags_and_email(self):
        legacy = User.objects.create_user(
            email="legacy-role-only@example.com",
            organization=None,
            password="LegacyPassword123!",
            name="Legacy Role Only",
            role=User.Role.SUPERADMIN,
        )
        self.assertFalse(legacy.is_superuser)
        self.assertFalse(legacy.is_staff)

        response = self.client.post(
            reverse(
                "admin:accounts_superadminaccount_change",
                args=[legacy.pk],
            ),
            {
                "email": "legacy-updated@example.com",
                "name": "Legacy Updated",
                "phone": "",
                "is_active": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        legacy.refresh_from_db()
        self.assertEqual(legacy.email, "legacy-updated@example.com")
        self.assertEqual(legacy.role, User.Role.SUPERADMIN)
        self.assertIsNone(legacy.organization_id)
        self.assertTrue(legacy.is_staff)
        self.assertTrue(legacy.is_superuser)

    def test_password_reset_and_delete_controls_are_available(self):
        other = User.objects.create_superuser(
            email="managed-superadmin@example.com",
            password="ManagedPassword123!",
            name="Managed Superadmin",
        )

        password_response = self.client.get(
            reverse(
                "admin:accounts_superadminaccount_password_change",
                args=[other.pk],
            )
        )
        self.assertEqual(password_response.status_code, 200)

        delete_response = self.client.post(
            reverse(
                "admin:accounts_superadminaccount_delete",
                args=[other.pk],
            ),
            {"post": "yes"},
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=other.pk).exists())
