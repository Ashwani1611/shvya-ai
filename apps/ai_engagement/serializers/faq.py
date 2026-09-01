from __future__ import annotations

from rest_framework import serializers

from apps.ai_engagement.models import FAQ


class FAQSerializer(serializers.ModelSerializer):
    """
    Serializer for organization-owned FAQs.

    Organization ownership is resolved server-side from the
    authenticated user and is therefore excluded here.
    """

    class Meta:
        model = FAQ

        fields = [
            "id",
            "question",
            "answer",
            "is_active",
            "created_at",
            "updated_at",
        ]

        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
        ]

    def validate_question(self, value):
        value = (value or "").strip()

        if not value:
            raise serializers.ValidationError(
                "FAQ question cannot be empty."
            )

        return value

    def validate_answer(self, value):
        value = (value or "").strip()

        if not value:
            raise serializers.ValidationError(
                "FAQ answer cannot be empty."
            )

        return value