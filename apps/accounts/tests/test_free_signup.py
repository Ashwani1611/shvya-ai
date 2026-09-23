from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.superadmin.forms import OrganizationUserUpdateForm, PipelineCreateForm


class FreeSignupTests(TestCase):
    def signup_post(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(*args, **kwargs)

    def setUp(self):
        cache.clear()
        self.payload = {
            "name": "Free Agent",
            "email": "free@example.com",
            "phone": "9876543210",
            "company_name": "Free Workspace",
            "password": "SecureSignupPassword123!",
            "package": "enterprise",
            "plan": "enterprise",
            "role": "admin",
        }

    @patch("apps.accounts.signup_delivery.send_mail", return_value=1)
    def test_signup_enforces_free_agent_and_owns_matching_pipeline(self, send_mail):
        response = self.signup_post(reverse("crm-signup"), self.payload)
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email=self.payload["email"])
        self.assertEqual(user.role, User.Role.AGENT)
        self.assertEqual(user.organization.package, Organization.Package.FREE)
        self.assertEqual(user.organization.plan, "free")
        self.assertEqual(user.phone, "+919876543210")
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        pipeline = user.organization.pipelines.get(name="Leads")
        self.assertEqual(pipeline.owner, user)
        from apps.crm.views.api import get_user_pipelines
        self.assertIn(pipeline, get_user_pipelines(user))
        self.assertEqual(pipeline.country_code, "+91")
        self.assertEqual(pipeline.country_code + pipeline.phone_number, user.phone)
        self.assertTrue(pipeline.stages.exists())
        send_mail.assert_called_once()

    def test_invalid_phone_does_not_create_organization(self):
        for phone in ["123456789", "12345678901", "+441234567890", "+9198765432100", "abcdefghij"]:
            with self.subTest(phone=phone):
                cache.clear()
                response = self.signup_post(reverse("crm-signup"), {**self.payload, "phone": phone})
                self.assertEqual(response.status_code, 400)
                self.assertIn("phone", response.context["errors"])
                self.assertFalse(Organization.objects.filter(name="Free Workspace").exists())

    def test_free_defaults_do_not_change_paid_pipeline_country(self):
        free = Organization.objects.create(name="Free", package="free")
        paid = Organization.objects.create(name="Paid", package="enterprise")
        self.assertEqual(free.pipelines.get(name="Leads").country_code, "+91")
        self.assertEqual(paid.pipelines.get(name="Leads").country_code, "")
        self.assertEqual(PipelineCreateForm(organization=free)["country_code"].value(), "+91")

    def test_free_organization_user_phone_is_normalized_on_edit(self):
        org = Organization.objects.create(name="Free", package="free")
        user = User.objects.create_user(email="edit@example.com", organization=org, role=User.Role.AGENT)
        form = OrganizationUserUpdateForm(
            {"name": "Agent", "email": user.email, "phone": "9876543210", "role": "agent", "is_active": True},
            instance=user, organization=org,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().phone, "+919876543210")

    def test_free_user_rejects_invalid_phone_but_paid_keeps_international_number(self):
        for package, valid in [("free", False), ("enterprise", True)]:
            with self.subTest(package=package):
                org = Organization.objects.create(name=package, package=package)
                user = User.objects.create_user(email=f"{package}@example.com", organization=org, role=User.Role.AGENT)
                form = OrganizationUserUpdateForm(
                    {"name": "Agent", "email": user.email, "phone": "+442079460123", "role": "agent", "is_active": True},
                    instance=user, organization=org,
                )
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.save().phone, "+442079460123")
                else:
                    self.assertIn("phone", form.errors)

    def test_free_pipeline_form_defaults_blank_country_to_india(self):
        org = Organization.objects.create(name="Free", package="free")
        form = PipelineCreateForm({"name": "Second", "country_code": "", "phone_number": "9876543210"}, organization=org)
        form.instance.organization = org
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["country_code"], "+91")
