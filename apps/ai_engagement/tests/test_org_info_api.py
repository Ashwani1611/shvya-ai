from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.ai_engagement.models import OrgInfo
from apps.organizations.models import Organization


User = get_user_model()


class OrgInfoAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="OrgInfo API Organization",
        )

        cls.user = User.objects.create_user(
            email="orginfo-api@example.com",
            password="test-password-123",
            organization=cls.organization,
        )

        cls.url = reverse("ai-org-info")

    def setUp(self):
        self.client.force_authenticate(
            user=self.user,
        )

    # ========================================================
    # GET
    # ========================================================

    def test_get_org_info_returns_engagement_instructions(self):
        OrgInfo.objects.create(
            organization=self.organization,
            about="Cybersecurity academy.",
            bot_languages="English, Hindi",
            qualification_requirements=(
                "Identify course interest and budget."
            ),
            engagement_instructions=(
                "Be helpful, concise, and professional."
            ),
            ai_enabled=True,
            bump_up_enabled=True,
            bump_up_count=2,
        )

        response = self.client.get(
            self.url,
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "Be helpful, concise, and professional.",
        )

    # ========================================================
    # PATCH
    # ========================================================

    def test_patch_updates_engagement_instructions(self):
        response = self.client.patch(
            self.url,
            {
                "engagement_instructions": (
                    "Speak naturally and professionally."
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "Speak naturally and professionally.",
        )

        org_info = OrgInfo.objects.get(
            organization=self.organization,
        )

        self.assertEqual(
            org_info.engagement_instructions,
            "Speak naturally and professionally.",
        )

    # ========================================================
    # PATCH TRIMS TEXT
    # ========================================================

    def test_patch_trims_engagement_instructions(self):
        response = self.client.patch(
            self.url,
            {
                "engagement_instructions": (
                    "   Be concise and helpful.   "
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "Be concise and helpful.",
        )

    # ========================================================
    # EMPTY VALUE
    # ========================================================

    def test_empty_engagement_instructions_is_allowed(self):
        response = self.client.patch(
            self.url,
            {
                "engagement_instructions": "",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "",
        )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_get_returns_only_authenticated_users_organization(self):
        other_organization = Organization.objects.create(
            name="Other Organization",
        )

        OrgInfo.objects.create(
            organization=self.organization,
            engagement_instructions=(
                "Organization A instructions."
            ),
        )

        OrgInfo.objects.create(
            organization=other_organization,
            engagement_instructions=(
                "Organization B instructions."
            ),
        )

        response = self.client.get(
            self.url,
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "Organization A instructions.",
        )

    # ========================================================
    # ORGANIZATION FIELD IS NOT ACCEPTED
    # ========================================================

    def test_organization_id_is_not_returned_or_modified(self):
        response = self.client.patch(
            self.url,
            {
                "organization_id": str(
                    self.organization.id,
                ),
                "engagement_instructions": (
                    "Valid engagement instructions."
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertEqual(
            response.data["engagement_instructions"],
            "Valid engagement instructions.",
        )

        org_info = OrgInfo.objects.get(
            organization=self.organization,
        )

        self.assertEqual(
            org_info.organization_id,
            self.organization.id,
        )