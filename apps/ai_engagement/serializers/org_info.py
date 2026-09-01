from __future__ import annotations

from rest_framework import serializers

from apps.ai_engagement.models import OrgInfo


class OrgInfoSerializer(
    serializers.ModelSerializer
):
    """
    Serializer for organization-level AI configuration.

    Organization ownership is deliberately excluded from the
    serializer. The authenticated user's organization determines
    ownership server-side.
    """

    class Meta:

        model = OrgInfo

        fields = [
            "id",
            "about",
            "bot_languages",
            "qualification_requirements",
            "engagement_instructions",
            "ai_enabled",
            "bump_up_enabled",
            "bump_up_count",
            "created_at",
            "updated_at",
        ]

        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
        ]

    def validate_bump_up_count(
        self,
        value,
    ):
        if value < 0:
            raise serializers.ValidationError(
                "Bump-up count cannot be negative."
            )

        return value