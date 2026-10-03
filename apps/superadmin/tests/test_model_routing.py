from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.views.org_info import OrgInfoView
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog


class OrganizationModelRoutingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Model routing org")
        self.other = Organization.objects.create(name="Other org")
        self.info = OrgInfo.objects.create(organization=self.organization, qualification_model="existing-model")
        self.admin = User.objects.create_superuser(email="routing-admin@example.com", password="test-password", name="Admin")
        self.member = User.objects.create_user(email="routing-member@example.com", password="test-password", name="Member", organization=self.organization)
        self.url = reverse("superadmin-organization-model-routing-update", kwargs={"organization_id": self.organization.pk})

    def _login(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

    def test_superadmin_updates_only_selected_organization_and_audits(self):
        self._login(self.admin)
        response = self.client.post(self.url, {"qualification_model": "new-model", "sales_support_model": "sales-model", "summary_model": ""})
        self.assertEqual(response.status_code, 302)
        self.info.refresh_from_db()
        self.assertEqual(self.info.qualification_model, "new-model")
        self.assertFalse(OrgInfo.objects.filter(organization=self.other).exists())
        self.assertEqual(AuditLog.objects.get().metadata["operation"], "ai_model_routing")
        page = self.client.get(reverse("superadmin-organization-detail", kwargs={"organization_id": self.organization.pk}))
        self.assertContains(page, 'value="new-model"')

    def test_organization_member_cannot_use_superadmin_endpoint(self):
        self._login(self.member)
        self.client.post(self.url, {"qualification_model": "hijacked-model"})
        self.info.refresh_from_db()
        self.assertEqual(self.info.qualification_model, "existing-model")
        self.assertFalse(AuditLog.objects.exists())

    def test_invalid_model_does_not_partially_save(self):
        self._login(self.admin)
        self.client.post(self.url, {"qualification_model": "new-model", "sales_support_model": "invalid model"})
        self.info.refresh_from_db()
        self.assertEqual(self.info.qualification_model, "existing-model")
        self.assertFalse(AuditLog.objects.exists())

    def test_organization_api_rejects_model_changes(self):
        request = APIRequestFactory().patch("/", {"qualification_model": "hijacked-model"}, format="json")
        force_authenticate(request, user=self.member)
        response = OrgInfoView.as_view()(request)
        self.assertEqual(response.status_code, 400)
        self.info.refresh_from_db()
        self.assertEqual(self.info.qualification_model, "existing-model")
