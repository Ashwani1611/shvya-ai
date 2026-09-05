from rest_framework import serializers


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


class PlaygroundResponseSerializer(
    serializers.Serializer
):
    session_id = serializers.CharField()
    message = serializers.CharField()
    response = serializers.CharField()
    should_engage = serializers.BooleanField()
    knowledge = serializers.ListField()
    model = serializers.CharField()