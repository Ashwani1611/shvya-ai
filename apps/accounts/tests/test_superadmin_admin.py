from django.contrib.auth.models import Group, Permission
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

    def test_editing_role_only_superadmin_preserves_authority_and_updates_email(self):
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
        self.assertEqual(legacy.name, "Legacy Updated")
        self.assertEqual(legacy.role, User.Role.SUPERADMIN)
        self.assertIsNone(legacy.organization_id)
        self.assertFalse(legacy.is_staff)
        self.assertFalse(legacy.is_superuser)

    def test_deactivating_restricted_editor_does_not_promote_platform_access(self):
        editor = User.objects.create_user(
            email="restricted-editor@example.com", name="Restricted Editor",
            password="EditorPassword123!", role=User.Role.SUPERADMIN,
        )
        permission = Permission.objects.get(content_type__app_label="blog", codename="change_post")
        editor.user_permissions.add(permission)
        response = self.client.post(
            reverse("admin:accounts_superadminaccount_change", args=[editor.pk]),
            {"email": editor.email, "name": "Former Editor", "phone": "", "_save": "Save"},
        )

        self.assertEqual(response.status_code, 302)
        editor.refresh_from_db()
        self.assertEqual(editor.name, "Former Editor")
        self.assertFalse(editor.is_active)
        self.assertFalse(editor.is_staff)
        self.assertFalse(editor.is_superuser)
        self.assertIsNone(editor.organization_id)
        self.assertEqual(editor.role, User.Role.SUPERADMIN)
        self.assertEqual(list(editor.user_permissions.values_list("pk", flat=True)), [permission.pk])

    def test_superuser_can_assign_editor_permissions_and_groups_in_users(self):
        editor = User.objects.create_user(
            email="grant-editor@example.com", name="Editor", role=User.Role.SUPERADMIN,
        )
        permission = Permission.objects.get(content_type__app_label="blog", codename="change_post")
        group = Group.objects.create(name="Website Publishers")
        group.permissions.add(Permission.objects.get(content_type__app_label="blog", codename="publish_post"))
        url = reverse("admin:accounts_user_change", args=[editor.pk])
        response = self.client.get(url)
        self.assertContains(response, 'name="user_permissions"')
        self.assertContains(response, 'name="groups"')
        response = self.client.post(url, {
            "email": editor.email, "name": editor.name, "phone": "", "organization": "",
            "role": User.Role.SUPERADMIN, "is_active": "on", "_save": "Save",
            "user_permissions": [permission.pk], "groups": [group.pk],
        })

        self.assertEqual(response.status_code, 302)
        editor.refresh_from_db()
        self.assertFalse(editor.is_superuser)
        self.assertFalse(editor.is_staff)
        self.assertTrue(editor.has_perm("blog.change_post"))
        self.assertTrue(editor.has_perm("blog.publish_post"))
        self.assertEqual(list(editor.groups.values_list("pk", flat=True)), [group.pk])

    def test_non_superuser_cannot_assign_permissions_or_groups_with_forged_post(self):
        manager = User.objects.create_user(
            email="user-manager@example.com", name="User Manager", role=User.Role.SUPERADMIN,
            is_staff=True,
        )
        manager.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="change_user"))
        editor = User.objects.create_user(
            email="unchanged-editor@example.com", name="Editor", role=User.Role.SUPERADMIN,
        )
        existing_permission = Permission.objects.get(content_type__app_label="blog", codename="change_post")
        publishing_permission = Permission.objects.get(content_type__app_label="blog", codename="publish_post")
        editor.user_permissions.add(existing_permission)
        group = Group.objects.create(name="Forbidden Membership")
        group.permissions.add(publishing_permission)
        self.client.force_login(manager)
        url = reverse("admin:accounts_user_change", args=[editor.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="user_permissions"')
        self.assertNotContains(response, 'name="groups"')
        response = self.client.post(url, {
            "email": editor.email, "name": "Updated Editor", "phone": "", "organization": "",
            "role": User.Role.SUPERADMIN, "is_active": "on", "_save": "Save",
            "user_permissions": [publishing_permission.pk], "groups": [group.pk],
        })

        self.assertEqual(response.status_code, 302)
        editor.refresh_from_db()
        self.assertEqual(editor.name, "Updated Editor")
        self.assertEqual(list(editor.user_permissions.values_list("pk", flat=True)), [existing_permission.pk])
        self.assertFalse(editor.groups.exists())
        self.assertFalse(editor.is_superuser)

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
