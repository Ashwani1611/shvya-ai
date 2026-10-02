from rest_framework import serializers

from apps.crm.models import Lead


class PlaygroundRequestSerializer(
    serializers.Serializer
):
    session_id = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )

    message = serializers.CharField(
        max_length=4000,
        trim_whitespace=True,
    )

    stage_id = serializers.UUIDField(required=False, allow_null=True)
    channel = serializers.ChoiceField(choices=("sandbox", "whatsapp", "instagram"), required=False)
    lead_source = serializers.ChoiceField(
        choices=Lead._meta.get_field("lead_source").choices, required=False,
    )

    history = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )

    def validate_message(
        self,
        value,
    ):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Message is required."
            )

        return value


class PlaygroundResetSerializer(
    serializers.Serializer
):
    session_id = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )


class PlaygroundResponseSerializer(
    serializers.Serializer
):
    session_id = serializers.CharField()
    message = serializers.CharField()
    response = serializers.CharField()
    should_engage = serializers.BooleanField()
    knowledge = serializers.ListField()
    model = serializers.CharField()
    stage = serializers.DictField(required=False)
    events = serializers.ListField(required=False)
    files = serializers.ListField(required=False)
    channel = serializers.CharField(required=False)
    lead_source = serializers.CharField(required=False)
    execution_mode = serializers.CharField(required=False)
