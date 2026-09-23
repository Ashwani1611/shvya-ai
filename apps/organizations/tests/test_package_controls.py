from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, RequestFactory, TestCase
from django.urls import reverse
from django.contrib.sessions.backends.db import SessionStore
from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.core.context_processors import sidebar_nav
from apps.organizations.features import MODULE_LABELS, module_enabled
from apps.organizations.middleware import PackageAccessMiddleware
from apps.organizations.models import Organization, OrganizationTag
from apps.superadmin.models import AuditLog


class PackageRulesTests(SimpleTestCase):
    def test_exact_default_access_matrix(self):
        blocked = {
            "free": set(MODULE_LABELS),
            "diy": {"calendar", "calls", "sales", "instagram"},
            "dfy": {"calls", "sales"},
            "enterprise": set(),
        }
        for package, denied in blocked.items():
            org = SimpleNamespace(package=package, settings={})
            for module in MODULE_LABELS:
                with self.subTest(package=package, module=module):
                    self.assertEqual(module_enabled(org, module), module not in denied)

    def test_overrides_are_package_scoped_and_never_unlock_free(self):
        org = SimpleNamespace(
            package="dfy",
            settings={"package_module_grants": {"dfy": ["calls"], "free": ["calls"]}},
        )
        self.assertTrue(module_enabled(org, "calls"))
        org.package = "diy"
        self.assertFalse(module_enabled(org, "calls"))
        org.package = "free"
        self.assertFalse(module_enabled(org, "calls"))
        org.settings = {"package_module_grants": {"free": None}}
        self.assertFalse(module_enabled(org, "calls"))

    def test_denied_post_does_not_execute_view(self):
        request = RequestFactory().post("/dashboard/sales/send/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            organization=SimpleNamespace(package="diy", settings={}),
        )

        def view(request):
            self.fail("Blocked view executed")

        self.assertEqual(PackageAccessMiddleware(view)(request).status_code, 403)


class SuperadminPackageControlsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="plan-admin@example.com", password="test-pass-123", name="Admin"
        )
        self.org = Organization.objects.create(name="Plan org", package="diy")
        self.other = Organization.objects.create(name="Other org", package="enterprise")
        self.user = User.objects.create_user(
            email="plan-user@example.com",
            password="test-pass-123",
            organization=self.org,
            name="User",
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

    def test_superadmin_can_grant_and_revoke_per_organisation(self):
        url = reverse("superadmin-organization-modules", args=[self.org.id])
        self.assertEqual(
            self.client.post(url, {"modules": ["calendar", "sales"]}).status_code, 302
        )
        self.org.refresh_from_db()
        self.assertTrue(module_enabled(self.org, "sales"))
        self.assertFalse(module_enabled(self.org, "calls"))
        self.other.refresh_from_db()
        self.assertNotIn("package_module_grants", self.other.settings)
        self.client.post(url, {})
        self.org.refresh_from_db()
        self.assertFalse(module_enabled(self.org, "sales"))

    def test_organisation_user_cannot_change_modules(self):
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key
        response = self.client.post(
            reverse("superadmin-organization-modules", args=[self.org.id]),
            {"modules": ["sales"]},
        )
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(module_enabled(self.org, "sales"))

    def test_delete_requires_confirmation_and_preserves_other_org(self):
        url = reverse("superadmin-organization-delete", args=[self.org.id])
        self.assertContains(self.client.get(url), "Confirm permanent deletion")
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        self.assertEqual(
            self.client.post(url, {"confirmation": str(self.other.pk)}).status_code, 400
        )
        self.assertEqual(
            self.client.post(url, {"confirmation": str(self.org.pk)}).status_code, 302
        )
        self.assertFalse(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Organization.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action="organization_deleted", target_id=str(self.org.pk)
            ).exists()
        )

    def test_global_tags_create_rename_delete(self):
        url = reverse("superadmin-tags")
        self.client.post(url, {"action": "create", "name": "Priority customer"})
        tag = OrganizationTag.objects.get(name="Priority customer")
        self.org.tags.add(tag)
        self.client.post(url, {"action": "edit", "tag_id": tag.pk, "name": "VIP"})
        self.assertEqual(self.org.tags.get().name, "VIP")
        self.assertEqual(
            self.client.post(url, {"action": "delete", "tag_id": tag.pk}).status_code,
            400,
        )
        self.client.post(
            url, {"action": "delete", "tag_id": tag.pk, "confirmation": str(tag.pk)}
        )
        self.assertFalse(self.org.tags.exists())

    def test_paid_sidebar_hides_restricted_modules_free_keeps_links(self):
        request = RequestFactory().get("/dashboard/")
        request.user = self.user
        with (
            patch("apps.core.context_processors._ai_credit_context", return_value={}),
            patch(
                "apps.core.context_processors._operations_support_context",
                return_value={},
            ),
        ):
            labels = {entry["label"] for entry in sidebar_nav(request)["nav_items"]}
            self.assertNotIn("SHVYA Sales", labels)
            self.assertNotIn("Instagram", labels)
            self.assertIn("CRM", labels)
            self.org.package = "free"
            self.user.organization = self.org
            entries = {
                entry["label"]: entry for entry in sidebar_nav(request)["nav_items"]
            }
            self.assertTrue(entries["SHVYA Sales"]["locked"])
            self.assertTrue(entries["WhatsApp"]["href"])
            self.assertNotIn("children", entries["WhatsApp"])

    def test_populated_org_deletion_preserves_audit_and_queues_external_cleanup(self):
        from apps.crm.models import Pipeline, Stage
        from apps.shvya_calendar.models import CalendarPage
        from apps.integrations.operations_models import OperationsAuditEvent
        from apps.integrations.diagnostic_models import DiagnosticAccessLog
        from apps.organizations.models import OrganizationDeletionCleanup
        from apps.support.models import (
            Ticket,
            TicketCategory,
            TicketIssue,
            TicketStatus,
            TicketPriority,
        )
        from apps.channels.models import WhatsAppAccount

        pipeline = Pipeline.objects.create(
            organization=self.org, name="Sales", owner=self.user
        )
        stage = Stage.objects.create(
            pipeline=pipeline, name="New Lead", display_order=0
        )
        CalendarPage.objects.create(
            organization=self.org,
            host=self.user,
            pipeline=pipeline,
            stage=stage,
            name="Demo",
            slug="demo",
            logo_file="calendar/logos/deleted-logo.png",
        )
        category = TicketCategory.objects.create(name="Support")
        issue = TicketIssue.objects.create(name="General", category=category)
        status = TicketStatus.objects.create(name="New", key="new", behavior="new")
        priority = TicketPriority.objects.create(name="Normal", key="normal")
        Ticket.objects.create(
            organization=self.org,
            requester=self.user,
            subject="Help",
            category=category,
            issue=issue,
            status=status,
            priority=priority,
        )
        account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type="hosted", business_name="Hosted"
        )
        event = OperationsAuditEvent.objects.create(
            actor=self.user,
            organization=self.org,
            role="admin",
            tool_name="read",
            outcome="success",
            request_fingerprint="a" * 64,
        )
        diagnostic = DiagnosticAccessLog.objects.create(
            organization=self.org, tool_name="read", request_fingerprint="b" * 64
        )
        response = self.client.post(
            reverse("superadmin-organization-delete", args=[self.org.pk]),
            {"confirmation": str(self.org.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertFalse(Ticket.objects.filter(organization_id=self.org.pk).exists())
        event.refresh_from_db()
        self.assertIsNone(event.organization_id)
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.organization_reference, self.org.pk)
        self.assertEqual(event.actor_reference, self.user.pk)
        diagnostic.refresh_from_db()
        self.assertIsNone(diagnostic.organization_id)
        self.assertEqual(diagnostic.organization_reference, self.org.pk)
        cleanup = OrganizationDeletionCleanup.objects.get(organization_id=self.org.pk)
        self.assertEqual(
            cleanup.hosted_session_ids,
            [{"id": str(account.pk), "shard": "primary"}],
        )
        self.assertIn(
            "calendar/logos/deleted-logo.png",
            [entry["name"] for entry in cleanup.files],
        )
        self.assertTrue(TicketCategory.objects.filter(pk=category.pk).exists())

    def test_cleanup_retries_failed_storage_without_losing_job(self):
        from apps.organizations.models import OrganizationDeletionCleanup
        from apps.organizations.tasks import cleanup_deleted_organizations

        job = OrganizationDeletionCleanup.objects.create(
            organization_id=self.org.pk,
            files=[
                {
                    "model": "shvya_calendar.CalendarPage",
                    "field": "logo_file",
                    "name": "calendar/logos/test.png",
                }
            ],
        )
        with patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("offline"),
        ):
            cleanup_deleted_organizations()
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        with patch("django.core.files.storage.FileSystemStorage.delete") as delete:
            cleanup_deleted_organizations()
        delete.assert_called_once_with("calendar/logos/test.png")
        self.assertFalse(OrganizationDeletionCleanup.objects.filter(pk=job.pk).exists())

    def test_changing_package_resets_previous_overrides(self):
        self.org.settings = {"package_module_grants": {"diy": ["sales"]}}
        self.org.save()
        self.assertTrue(module_enabled(self.org, "sales"))
        self.org.package = "dfy"
        self.org.save(update_fields=["package"])
        self.org.refresh_from_db()
        self.assertFalse(module_enabled(self.org, "sales"))
        self.org.package = "diy"
        self.org.save(update_fields=["package"])
        self.org.refresh_from_db()
        self.assertFalse(module_enabled(self.org, "sales"))

    def test_free_dashboard_links_show_upgrade_and_mutations_are_denied(self):
        self.org.package = 'free'
        self.org.save(update_fields=['package'])
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies['shvya_crm_sessionid'] = session.session_key
        response = self.client.get('/dashboard/sales/')
        self.assertContains(response, 'Upgrade to unlock', status_code=403)
        self.assertEqual(self.client.post('/dashboard/sales/', {}).status_code, 403)

    def test_csrf_required_for_deletion(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.cookies['shvya_superadmin_sessionid'] = self.client.cookies['shvya_superadmin_sessionid']
        response = client.post(reverse('superadmin-organization-delete', args=[self.org.pk]), {'confirmation': str(self.org.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
