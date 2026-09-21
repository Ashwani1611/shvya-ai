from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.organizations.access import crm_user_is_authorized

from ..models import CallDevice
from ..serializers import (
    CallEventIngestSerializer,
    CallFilterSerializer,
    CallFollowUpSerializer,
    CallNotesSerializer,
    CallRecordSerializer,
    DeviceHeartbeatSerializer,
    DeviceRegistrationSerializer,
)
from ..services import (
    get_call_settings,
    ingest_call_event,
    set_call_follow_up,
    update_call_notes,
    visible_calls,
    visible_pipelines,
)


def _authorized_user(request):
    user = request.user
    return bool(
        user
        and user.is_authenticated
        and getattr(user, "organization_id", None)
        and crm_user_is_authorized(user)
    )


class CallIntelligenceAPIView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not _authorized_user(request):
            self.permission_denied(
                request,
                message="This organization user cannot access Call Intelligence.",
            )


class DeviceRegisterAPIView(CallIntelligenceAPIView):
    def post(self, request):
        serializer = DeviceRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        device, created = CallDevice.objects.update_or_create(
            organization=request.user.organization,
            device_uuid=data["device_id"],
            defaults={
                "user": request.user,
                "device_name": data.get("device_name", ""),
                "manufacturer": data.get("manufacturer", ""),
                "model": data.get("model", ""),
                "android_version": data.get("android_version", ""),
                "app_version": data.get("app_version", ""),
                "permissions": data.get("permissions") or {},
                "battery_optimization_ignored": data.get(
                    "battery_optimization_ignored",
                    False,
                ),
                "is_active": True,
            },
        )
        return Response(
            {
                "device_id": str(device.device_uuid),
                "registered": created,
                "server_device_id": str(device.id),
            },
            status=status.HTTP_200_OK,
        )


class DeviceHeartbeatAPIView(CallIntelligenceAPIView):
    def post(self, request):
        serializer = DeviceHeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = CallDevice.objects.filter(
            organization=request.user.organization,
            user=request.user,
            device_uuid=data["device_id"],
            is_active=True,
        ).first()
        if device is None:
            return Response(
                {"message": "Register this Android device first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        update_fields = ["last_seen_at"]
        if "app_version" in data:
            device.app_version = data["app_version"]
            update_fields.append("app_version")
        if "permissions" in data:
            device.permissions = data["permissions"]
            update_fields.append("permissions")
        if "battery_optimization_ignored" in data:
            device.battery_optimization_ignored = data[
                "battery_optimization_ignored"
            ]
            update_fields.append("battery_optimization_ignored")
        device.last_seen_at = timezone.now()
        device.save(update_fields=update_fields)
        return Response({"ok": True})


class CallEventIngestAPIView(CallIntelligenceAPIView):
    def post(self, request):
        serializer = CallEventIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            call, created = ingest_call_event(
                user=request.user,
                validated_data=serializer.validated_data,
            )
        except DjangoValidationError as exc:
            payload = (
                exc.message_dict
                if hasattr(exc, "message_dict")
                else {"message": exc.messages}
            )
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)

        call = visible_calls(request.user).filter(pk=call.pk).first()
        if call is None:
            return Response(
                {"message": "Call was accepted but is outside this user's view."},
                status=status.HTTP_202_ACCEPTED,
            )
        return Response(
            {
                "created": created,
                "call": CallRecordSerializer(call).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CallListAPIView(CallIntelligenceAPIView):
    def get(self, request):
        filters = CallFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data

        qs = visible_calls(request.user)
        if data.get("status"):
            qs = qs.filter(status=data["status"])
        if data.get("direction"):
            qs = qs.filter(direction=data["direction"])
        if data.get("lead"):
            qs = qs.filter(lead_id=data["lead"])
        search = str(data.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(phone_number__icontains=search)
                | Q(contact_name__icontains=search)
                | Q(lead__name__icontains=search)
            )

        limit = min(max(int(request.query_params.get("limit", 50)), 1), 200)
        rows = qs[:limit]
        return Response(
            {
                "count": qs.count(),
                "results": CallRecordSerializer(rows, many=True).data,
            }
        )


class CallDetailAPIView(CallIntelligenceAPIView):
    def get_object(self, request, call_id):
        return visible_calls(request.user).filter(pk=call_id).first()

    def get(self, request, call_id):
        call = self.get_object(request, call_id)
        if call is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(CallRecordSerializer(call).data)


class CallNotesAPIView(CallDetailAPIView):
    def post(self, request, call_id):
        call = self.get_object(request, call_id)
        if call is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = CallNotesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        update_call_notes(
            call=call,
            notes=serializer.validated_data.get("notes", ""),
        )
        return Response(CallRecordSerializer(call).data)


class CallFollowUpAPIView(CallDetailAPIView):
    def post(self, request, call_id):
        call = self.get_object(request, call_id)
        if call is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = CallFollowUpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reminder = set_call_follow_up(
                call=call,
                assigned_to=request.user,
                due_at=serializer.validated_data["due_at"],
                title=serializer.validated_data.get("title", ""),
                description=serializer.validated_data.get("description", ""),
            )
        except DjangoValidationError as exc:
            return Response(
                {"message": exc.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "reminder_id": str(reminder.id),
                "due_at": reminder.due_at,
            }
        )


class CallBootstrapAPIView(CallIntelligenceAPIView):
    def get(self, request):
        settings_obj = get_call_settings(request.user.organization)
        pipelines = []
        for pipeline in visible_pipelines(request.user).prefetch_related("stages"):
            pipelines.append(
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "stages": [
                        {
                            "id": str(stage.id),
                            "name": stage.name,
                            "display_order": stage.display_order,
                        }
                        for stage in pipeline.stages.filter(is_active=True)
                    ],
                }
            )

        return Response(
            {
                "user": {
                    "id": str(request.user.id),
                    "name": request.user.name,
                    "email": request.user.email,
                    "role": request.user.role,
                    "organization_id": str(request.user.organization_id),
                    "organization": request.user.organization.name,
                },
                "settings": {
                    "enabled": settings_obj.is_enabled,
                    "auto_create_answered": settings_obj.auto_create_answered,
                    "auto_create_missed": settings_obj.auto_create_missed,
                    "auto_create_outbound": settings_obj.auto_create_outbound,
                    "default_country_code": settings_obj.default_country_code,
                    "default_pipeline_id": (
                        str(settings_obj.default_pipeline_id)
                        if settings_obj.default_pipeline_id
                        else None
                    ),
                    "default_stage_id": (
                        str(settings_obj.default_stage_id)
                        if settings_obj.default_stage_id
                        else None
                    ),
                },
                "pipelines": pipelines,
            }
        )
