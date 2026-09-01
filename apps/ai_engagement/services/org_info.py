from __future__ import annotations

from django.core.exceptions import ValidationError

from apps.ai_engagement.models import OrgInfo


class OrgInfoServiceError(Exception):
    """
    Raised when organization AI configuration cannot be safely
    created, retrieved, or updated.
    """


class OrgInfoService:
    """
    Service layer for organization-level AI configuration.

    One Organization owns exactly one OrgInfo record.

    Views/API layers should use this service instead of directly
    implementing OrgInfo business logic.
    """

    ALLOWED_FIELDS = {
        "about",
        "bot_languages",
        "qualification_requirements",
        "engagement_instructions",
        "ai_enabled",
        "bump_up_enabled",
        "bump_up_count",
    }

    # ========================================================
    # GET / CREATE
    # ========================================================

    def get_or_create(
        self,
        *,
        organization,
    ) -> OrgInfo:
        """
        Return the organization's OrgInfo record.

        Creates the default configuration when the organization
        has not configured AI yet.
        """

        if organization is None:
            raise OrgInfoServiceError(
                "Organization is required."
            )

        org_info, _created = (
            OrgInfo.objects.get_or_create(
                organization=organization,
            )
        )

        return org_info

    # ========================================================
    # UPDATE
    # ========================================================

    def update(
        self,
        *,
        organization,
        data: dict,
    ) -> OrgInfo:
        """
        Update the organization's AI configuration.

        Only fields belonging to OrgInfo are accepted.
        """

        if organization is None:
            raise OrgInfoServiceError(
                "Organization is required."
            )

        unknown_fields = (
            set(data.keys())
            - self.ALLOWED_FIELDS
        )

        if unknown_fields:
            raise OrgInfoServiceError(
                "Unsupported AI configuration fields: "
                + ", ".join(
                    sorted(
                        unknown_fields,
                    )
                )
            )

        org_info = self.get_or_create(
            organization=organization,
        )

        # ----------------------------------------------------
        # TEXT CONFIGURATION
        # ----------------------------------------------------

        if "about" in data:

            org_info.about = (
                data["about"] or ""
            ).strip()

        if "bot_languages" in data:

            org_info.bot_languages = (
                data["bot_languages"] or ""
            ).strip()

        if "qualification_requirements" in data:

            org_info.qualification_requirements = (
                data["qualification_requirements"] or ""
            ).strip()

        if "engagement_instructions" in data:

            org_info.engagement_instructions = (
                data["engagement_instructions"] or ""
            ).strip()

        # ----------------------------------------------------
        # AI MASTER SWITCH
        # ----------------------------------------------------

        if "ai_enabled" in data:

            org_info.ai_enabled = (
                data["ai_enabled"]
            )

        # ----------------------------------------------------
        # BUMP-UP SETTINGS
        # ----------------------------------------------------

        if "bump_up_enabled" in data:

            org_info.bump_up_enabled = (
                data["bump_up_enabled"]
            )

        if "bump_up_count" in data:

            bump_up_count = (
                data["bump_up_count"]
            )

            if (
                isinstance(
                    bump_up_count,
                    bool,
                )
                or not isinstance(
                    bump_up_count,
                    int,
                )
            ):
                raise OrgInfoServiceError(
                    "bump_up_count must be a non-negative integer."
                )

            if bump_up_count < 0:
                raise OrgInfoServiceError(
                    "bump_up_count cannot be negative."
                )

            org_info.bump_up_count = (
                bump_up_count
            )

        # ----------------------------------------------------
        # MODEL VALIDATION
        # ----------------------------------------------------

        try:

            org_info.full_clean(
                validate_unique=False,
            )

        except ValidationError as exc:

            if hasattr(
                exc,
                "message_dict",
            ):

                raise OrgInfoServiceError(
                    exc.message_dict
                ) from exc

            raise OrgInfoServiceError(
                str(exc)
            ) from exc

        org_info.save()

        return org_info