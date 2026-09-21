from rest_framework import serializers

from .models import CallDevice, CallEvent, CallRecord


class DeviceRegistrationSerializer(serializers.Serializer):
    device_id = serializers.UUIDField()
    device_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    manufacturer = serializers.CharField(required=False, allow_blank=True, max_length=100)
    model = serializers.CharField(required=False, allow_blank=True, max_length=100)
    android_version = serializers.CharField(required=False, allow_blank=True, max_length=40)
    app_version = serializers.CharField(required=False, allow_blank=True, max_length=40)
    permissions = serializers.JSONField(required=False)
    battery_optimization_ignored = serializers.BooleanField(required=False)


class DeviceHeartbeatSerializer(serializers.Serializer):
    device_id = serializers.UUIDField()
    app_version = serializers.CharField(required=False, allow_blank=True, max_length=40)
    permissions = serializers.JSONField(required=False)
    battery_optimization_ignored = serializers.BooleanField(required=False)


class CallEventIngestSerializer(serializers.Serializer):
    device_id = serializers.UUIDField()
    event_uuid = serializers.UUIDField()
    source_call_id = serializers.CharField(required=False, allow_blank=True, max_length=160)
    event_type = serializers.ChoiceField(choices=CallEvent.Type.choices)
    direction = serializers.ChoiceField(
        choices=CallRecord.Direction.choices,
        required=False,
    )
    status = serializers.ChoiceField(
        choices=CallRecord.Status.choices,
        required=False,
    )
    phone_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    raw_phone_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    contact_name = serializers.CharField(required=False, allow_blank=True, max_length=180)
    sim_slot = serializers.CharField(required=False, allow_blank=True, max_length=30)
    occurred_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    ringing_at = serializers.DateTimeField(required=False, allow_null=True)
    answered_at = serializers.DateTimeField(required=False, allow_null=True)
    ended_at = serializers.DateTimeField(required=False, allow_null=True)
    ring_duration_seconds = serializers.IntegerField(required=False, min_value=0)
    duration_seconds = serializers.IntegerField(required=False, min_value=0)
    metadata = serializers.JSONField(required=False)
    payload = serializers.JSONField(required=False)

    def validate(self, attrs):
        if not (attrs.get("phone_number") or attrs.get("raw_phone_number")):
            raise serializers.ValidationError("phone_number or raw_phone_number is required.")
        return attrs


class CallRecordSerializer(serializers.ModelSerializer):
    lead = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()
    intelligence = serializers.SerializerMethodField()

    class Meta:
        model = CallRecord
        fields = [
            "id",
            "source",
            "source_call_id",
            "direction",
            "status",
            "lead_match_status",
            "phone_number",
            "raw_phone_number",
            "contact_name",
            "sim_slot",
            "started_at",
            "ringing_at",
            "answered_at",
            "ended_at",
            "called_at",
            "ring_duration_seconds",
            "duration_seconds",
            "notes",
            "disposition",
            "follow_up_required",
            "follow_up_at",
            "lead",
            "user",
            "intelligence",
            "created_at",
            "updated_at",
        ]

    def get_lead(self, obj):
        if obj.lead_id is None:
            return None
        return {
            "id": str(obj.lead_id),
            "name": obj.lead.name,
            "phone": obj.lead.phone,
            "pipeline_id": str(obj.lead.pipeline_id),
            "pipeline": obj.lead.pipeline.name if obj.lead.pipeline_id else "",
            "stage_id": str(obj.lead.stage_id),
            "stage": obj.lead.stage.name if obj.lead.stage_id else "",
        }

    def get_user(self, obj):
        if obj.user_id is None:
            return None
        return {
            "id": str(obj.user_id),
            "name": obj.user.name,
            "email": obj.user.email,
        }

    def get_intelligence(self, obj):
        try:
            intelligence = obj.intelligence
        except Exception:
            return None
        return {
            "analysis_status": intelligence.analysis_status,
            "summary": intelligence.summary,
            "intent": intelligence.intent,
            "sentiment": intelligence.sentiment,
            "outcome": intelligence.outcome,
            "next_action": intelligence.next_action,
            "follow_up_at": intelligence.follow_up_at,
            "ai_score": (
                str(intelligence.ai_score)
                if intelligence.ai_score is not None
                else None
            ),
        }


class CallNotesSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True)


class CallFollowUpSerializer(serializers.Serializer):
    due_at = serializers.DateTimeField()
    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)


class CallFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=CallRecord.Status.choices, required=False)
    direction = serializers.ChoiceField(choices=CallRecord.Direction.choices, required=False)
    lead = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, allow_blank=True, max_length=100)
