
# --- migrated from serializers.py ---
from rest_framework import serializers


RESERVED_FIELDS = {
    "name",
    "phone",
    "email",
    "notes",
    "stage",
    "pipeline",
    "sequence",
    "attributes",
}


class LeadUpsertSerializer(
    serializers.Serializer
):

    name = serializers.CharField(
        required=True,
        max_length=150,
    )

    phone = serializers.CharField(
        required=True,
        max_length=32,
    )

    email = serializers.EmailField(
        required=False,
        allow_blank=True,
    )

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
    )

    stage = serializers.CharField(
        required=False,
        allow_blank=False,
    )

    pipeline = serializers.CharField(
        required=False,
        allow_blank=False,
    )

    sequence = serializers.CharField(
        required=False,
        allow_blank=True,
    )

    attributes = serializers.DictField(
        required=False,
        child=serializers.JSONField(),
    )

    def to_internal_value(self, data):

        declared_data = {
            key: value
            for key, value in data.items()
            if key in RESERVED_FIELDS
        }

        result = super().to_internal_value(
            declared_data
        )

        custom_attributes = {}

        if "attributes" in result:
            custom_attributes.update(
                result.pop("attributes")
            )

        for key, value in data.items():

            if key in RESERVED_FIELDS:
                continue

            custom_attributes[key] = value

        if "sequence" in result:
            custom_attributes["sequence"] = result.pop(
                "sequence"
            )

        result["attributes"] = custom_attributes

        return result