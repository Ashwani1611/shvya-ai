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
            "ai_playbook",
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
    def validate_ai_playbook(self, value):
        from apps.ai_engagement.services.playbook import validate_playbook
        try:
            return validate_playbook(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def to_internal_value(self, data):
        removed = {"qualification_requirements", "engagement_instructions"} & set(data)
        if removed:
            raise serializers.ValidationError({key: "Use ai_playbook instead." for key in removed})
        return super().to_internal_value(data)
