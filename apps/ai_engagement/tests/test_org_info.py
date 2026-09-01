from __future__ import annotations

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.org_info import (
    OrgInfoService,
    OrgInfoServiceError,
)
from apps.organizations.models import Organization


class OrgInfoServiceTests(
    TestCase
):

    @classmethod
    def setUpTestData(
        cls,
    ):

        cls.organization = (
            Organization.objects.create(
                name="OrgInfo Test Organization",
            )
        )

        cls.other_organization = (
            Organization.objects.create(
                name="Other Organization",
            )
        )

    # ========================================================
    # DEFAULT CREATION
    # ========================================================

    def test_get_or_create_creates_default_configuration(
        self,
    ):

        service = (
            OrgInfoService()
        )

        self.assertFalse(
            OrgInfo.objects.filter(
                organization=self.organization,
            ).exists(),
        )

        org_info = (
            service.get_or_create(
                organization=self.organization,
            )
        )

        self.assertEqual(
            org_info.organization_id,
            self.organization.id,
        )

        self.assertTrue(
            org_info.ai_enabled,
        )

        self.assertTrue(
            org_info.bump_up_enabled,
        )

        self.assertEqual(
            org_info.bump_up_count,
            2,
        )

        self.assertEqual(
            OrgInfo.objects.filter(
                organization=self.organization,
            ).count(),
            1,
        )

    # ========================================================
    # EXISTING CONFIGURATION
    # ========================================================

    def test_get_or_create_reuses_existing_configuration(
        self,
    ):

        existing = (
            OrgInfo.objects.create(
                organization=self.organization,
                about="Existing configuration",
                ai_enabled=False,
            )
        )

        service = (
            OrgInfoService()
        )

        result = (
            service.get_or_create(
                organization=self.organization,
            )
        )

        self.assertEqual(
            result.id,
            existing.id,
        )

        self.assertEqual(
            result.about,
            "Existing configuration",
        )

    # ========================================================
    # UPDATE
    # ========================================================

    def test_update_all_configuration_fields(
        self,
    ):

        service = (
            OrgInfoService()
        )

        result = service.update(
            organization=self.organization,
            data={
                "about": (
                    "Cybersecurity training academy."
                ),
                "bot_languages": (
                    "English, Hindi, Hinglish"
                ),
                "qualification_requirements": (
                    "Identify course interest, "
                    "experience, budget and timeline."
                ),
                "ai_enabled": False,
                "bump_up_enabled": False,
                "bump_up_count": 5,
            },
        )

        self.assertEqual(
            result.about,
            "Cybersecurity training academy.",
        )

        self.assertEqual(
            result.bot_languages,
            "English, Hindi, Hinglish",
        )

        self.assertEqual(
            result.qualification_requirements,
            (
                "Identify course interest, "
                "experience, budget and timeline."
            ),
        )

        self.assertFalse(
            result.ai_enabled,
        )

        self.assertFalse(
            result.bump_up_enabled,
        )

        self.assertEqual(
            result.bump_up_count,
            5,
        )

    # ========================================================
    # PARTIAL UPDATE
    # ========================================================

    def test_partial_update_preserves_other_fields(
        self,
    ):

        service = (
            OrgInfoService()
        )

        service.update(
            organization=self.organization,
            data={
                "about": "Original business",
                "bot_languages": "English",
                "qualification_requirements": (
                    "Original qualification rules"
                ),
            },
        )

        result = service.update(
            organization=self.organization,
            data={
                "ai_enabled": False,
            },
        )

        self.assertFalse(
            result.ai_enabled,
        )

        self.assertEqual(
            result.about,
            "Original business",
        )

        self.assertEqual(
            result.bot_languages,
            "English",
        )

        self.assertEqual(
            result.qualification_requirements,
            "Original qualification rules",
        )

    # ========================================================
    # UNKNOWN FIELDS
    # ========================================================

    def test_unknown_fields_are_rejected(
        self,
    ):

        service = (
            OrgInfoService()
        )

        with self.assertRaises(
            OrgInfoServiceError,
        ):

            service.update(
                organization=self.organization,
                data={
                    "organization_id": (
                        str(
                            self.other_organization.id
                        )
                    ),
                },
            )

    # ========================================================
    # BUMP-UP VALIDATION
    # ========================================================

    def test_negative_bump_up_count_is_rejected(
        self,
    ):

        service = (
            OrgInfoService()
        )

        with self.assertRaises(
            OrgInfoServiceError,
        ):

            service.update(
                organization=self.organization,
                data={
                    "bump_up_count": -1,
                },
            )

    def test_boolean_bump_up_count_is_rejected(
        self,
    ):

        service = (
            OrgInfoService()
        )

        with self.assertRaises(
            OrgInfoServiceError,
        ):

            service.update(
                organization=self.organization,
                data={
                    "bump_up_count": True,
                },
            )

    # ========================================================
    # AI ENABLE/DISABLE
    # ========================================================

    def test_ai_can_be_disabled(
        self,
    ):

        service = (
            OrgInfoService()
        )

        result = service.update(
            organization=self.organization,
            data={
                "ai_enabled": False,
            },
        )

        self.assertFalse(
            result.ai_enabled,
        )

    def test_ai_can_be_enabled(
        self,
    ):

        service = (
            OrgInfoService()
        )

        result = service.update(
            organization=self.organization,
            data={
                "ai_enabled": True,
            },
        )

        self.assertTrue(
            result.ai_enabled,
        )

    # ========================================================
    # BUMP-UP TOGGLE
    # ========================================================

    def test_bump_up_can_be_disabled(
        self,
    ):

        service = (
            OrgInfoService()
        )

        result = service.update(
            organization=self.organization,
            data={
                "bump_up_enabled": False,
            },
        )

        self.assertFalse(
            result.bump_up_enabled,
        )

    # ========================================================
    # ORGANIZATION ISOLATION
    # ========================================================

    def test_each_organization_has_separate_configuration(
        self,
    ):

        service = (
            OrgInfoService()
        )

        first = service.update(
            organization=self.organization,
            data={
                "about": "Organization A",
                "ai_enabled": False,
            },
        )

        second = service.update(
            organization=self.other_organization,
            data={
                "about": "Organization B",
                "ai_enabled": True,
            },
        )

        self.assertNotEqual(
            first.id,
            second.id,
        )

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(
            first.about,
            "Organization A",
        )

        self.assertEqual(
            second.about,
            "Organization B",
        )

        self.assertFalse(
            first.ai_enabled,
        )

        self.assertTrue(
            second.ai_enabled,
        )