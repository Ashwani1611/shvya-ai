from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.crm.models import LeadReminder
from apps.organizations.access import crm_user_is_authorized
from services.crm_activity_service import record_reminder_created

from ..models import CallAppRelease, CallDevice, CallRecord
from ..services import get_call_settings, ingest_call_event, register_device
from ..tasks import analyze_call_intelligence


def _user(request):
    user = request.user
    if not crm_user_is_authorized(user):
        raise PermissionDenied("Active organization user is required.")
    return user


def _error(exc):
    detail = exc.message_dict if hasattr(exc, "message_dict") else (
        getattr(exc, "messages", None) or [str(exc)]
    )
    return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)


def _call_queryset(user):
    qs = (
        CallRecord.objects
        .filter(organization=user.organization)
        .select_related("lead", "lead__pipeline", "lead__stage", "user", "crm_call", "intelligence")
    )
    if user.role == User.Role.AGENT:
        qs = qs.filter(user=user)
    return qs


def serialize_call(call):
    intelligence = getattr(call, "intelligence", None)
    return {
        "id": str(call.id),
        "source_call_id": call.source_call_id,
        "phone_number": call.phone_number,
        "contact_name": call.contact_name,
        "direction": call.direction,
        "status": call.status,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "ring_duration_seconds": call.ring_duration_seconds,
        "talk_duration_seconds": call.talk_duration_seconds,
        "total_duration_seconds": call.total_duration_seconds,
        "notes": call.notes,
        "disposition": call.disposition,
        "follow_up_required": call.follow_up_required,
        "follow_up_at": call.follow_up_at.isoformat() if call.follow_up_at else None,
        "lead": (
            {
                "id": str(call.lead_id),
                "name": call.lead.name,
                "pipeline": call.lead.pipeline.name,
                "stage": call.lead.stage.name,
            }
            if call.lead_id else None
        ),
        "intelligence": (
            {
                "summary": intelligence.summary,
                "intent": intelligence.intent,
                "sentiment": intelligence.sentiment,
                "outcome": intelligence.outcome,
                "objections": intelligence.objections,
                "buying_signals": intelligence.buying_signals,
                "competitor": intelligence.competitor,
                "budget": intelligence.budget,
                "timeline": intelligence.timeline,
                "next_action": intelligence.next_action,
                "attributes": intelligence.extracted_attributes,
                "analyzed_at": intelligence.analyzed_at.isoformat() if intelligence.analyzed_at else None,
            }
            if intelligence else None
        ),
    }


class DeviceRegistrationView(APIView):
    def post(self, request):
        try:
            device = register_device(user=_user(request), payload=request.data)
        except DjangoValidationError as exc:
            return _error(exc)
        return Response({
            "device": {
                "id": str(device.id),
                "device_id": device.device_id,
                "name": device.name,
                "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
            }
        })


class DeviceHeartbeatView(APIView):
    def post(self, request):
        user = _user(request)
        device_id = str(request.data.get("device_id") or "").strip()
        device = (
            CallDevice.objects
            .filter(
                organization=user.organization,
                user=user,
                device_id=device_id,
                is_active=True,
            )
            .first()
        )
        if device is None:
            return Response({"detail": "Device is not registered."}, status=404)
        device.last_seen_at = timezone.now()
        if isinstance(request.data.get("permissions"), dict):
            device.permissions = request.data["permissions"]
        if "battery_optimization_ignored" in request.data:
            device.battery_optimization_ignored = bool(
                request.data["battery_optimization_ignored"]
            )
        device.save(update_fields=[
            "last_seen_at", "permissions", "battery_optimization_ignored", "updated_at"
        ])
        return Response({"ok": True})


class CallEventView(APIView):
    def post(self, request):
        try:
            result = ingest_call_event(user=_user(request), payload=request.data)
        except DjangoValidationError as exc:
            return _error(exc)
        return Response(
            {
                "ok": True,
                "event_created": result["event_created"],
                "lead_created": result["lead_created"],
                "call": serialize_call(result["call"]),
            },
            status=201 if result["event_created"] else 200,
        )


class CallCollectionView(APIView):
    def get(self, request):
        user = _user(request)
        qs = _call_queryset(user)
        call_status = str(request.query_params.get("status") or "").strip()
        if call_status in CallRecord.Status.values:
            qs = qs.filter(status=call_status)
        direction = str(request.query_params.get("direction") or "").strip()
        if direction in CallRecord.Direction.values:
            qs = qs.filter(direction=direction)
        rows = list(qs.order_by("-ended_at", "-created_at")[:100])
        return Response({"calls": [serialize_call(row) for row in rows]})


class CallNotesView(APIView):
    def patch(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)

        notes = str(request.data.get("notes") or "").strip()[:20000]
        disposition = str(request.data.get("disposition") or "").strip()[:80]
        call.notes = notes
        call.disposition = disposition
        call.save(update_fields=["notes", "disposition", "updated_at"])
        if call.crm_call_id:
            call.crm_call.notes = notes
            call.crm_call.save(update_fields=["notes"])
        if notes:
            try:
                analyze_call_intelligence.delay(str(call.id))
            except Exception:
                pass
        return Response({"ok": True, "call": serialize_call(call)})


class CallFollowUpView(APIView):
    def post(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)
        if not call.lead_id:
            return Response(
                {"detail": "A CRM lead is required before setting a reminder."},
                status=400,
            )

        due_at = parse_datetime(str(request.data.get("due_at") or "").strip())
        if due_at is None:
            return Response(
                {"detail": "due_at must be a valid ISO-8601 timestamp."},
                status=400,
            )
        if timezone.is_naive(due_at):
            due_at = timezone.make_aware(due_at, timezone.get_current_timezone())

        title = str(request.data.get("title") or "").strip()[:200] or "Call follow-up"
        description = str(request.data.get("description") or "").strip()[:2000]

        with transaction.atomic():
            reminder = LeadReminder.objects.create(
                lead=call.lead,
                assigned_to=user,
                title=title,
                description=description,
                due_at=due_at,
                status="pending",
            )
            record_reminder_created(lead=call.lead, actor=user, reminder=reminder)
            call.follow_up_required = True
            call.follow_up_at = due_at
            call.save(update_fields=["follow_up_required", "follow_up_at", "updated_at"])

        return Response({
            "ok": True,
            "reminder_id": str(reminder.id),
            "due_at": reminder.due_at.isoformat(),
        }, status=201)


class CallSettingsView(APIView):
    def get(self, request):
        user = _user(request)
        settings_obj = get_call_settings(user.organization)
        release = CallAppRelease.objects.filter(is_active=True).first()
        return Response({
            "enabled": settings_obj.enabled,
            "auto_create_answered_incoming": settings_obj.auto_create_answered_incoming,
            "auto_create_answered_outgoing": settings_obj.auto_create_answered_outgoing,
            "auto_create_missed": settings_obj.auto_create_missed,
            "auto_create_rejected": settings_obj.auto_create_rejected,
            "default_pipeline_id": str(settings_obj.default_pipeline_id) if settings_obj.default_pipeline_id else None,
            "default_stage_id": str(settings_obj.default_stage_id) if settings_obj.default_stage_id else None,
            "default_owner_id": str(settings_obj.default_owner_id) if settings_obj.default_owner_id else None,
            "release": (
                {
                    "version_name": release.version_name,
                    "version_code": release.version_code,
                    "download_url": release.download_url,
                    "sha256": release.sha256,
                    "mandatory": release.is_mandatory,
                }
                if release else None
            ),
        })
