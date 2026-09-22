import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.crm.models import LeadReminder
from apps.organizations.access import crm_user_is_authorized
from services.crm_activity_service import record_reminder_created

from ..models import (
    CallAppRelease,
    CallDevice,
    CallDisposition,
    CallEvent,
    CallIntelligenceResult,
    CallRecord,
)
from ..services import (
    TERMINAL_EVENTS,
    get_call_dispositions,
    get_call_settings,
    ingest_call_event,
    register_device,
)
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
        .select_related(
            "lead", "lead__pipeline", "lead__stage", "user", "crm_call", "intelligence"
        )
    )
    if user.role == User.Role.AGENT:
        qs = qs.filter(user=user)
    return qs


def serialize_call(call):
    intelligence = getattr(call, "intelligence", None)
    return {
        "id": str(call.id),
        "source": call.source,
        "source_call_id": call.source_call_id,
        "provider": call.provider,
        "provider_call_id": call.provider_call_id,
        "sim_account_id": call.sim_account_id,
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
        "recording_url": call.recording_url,
        "recording_status": call.recording_status,
        "transcript_status": call.transcript_status,
        "transcript": call.transcript,
        "transcript_speakers": call.transcript_speakers,
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
                "product_interest": intelligence.product_interest,
                "decision_maker": intelligence.decision_maker,
                "follow_up_at": intelligence.follow_up_at.isoformat()
                if intelligence.follow_up_at else None,
                "next_action": intelligence.next_action,
                "ai_score": intelligence.ai_score,
                "qualification_score": intelligence.qualification_score,
                "agent_metrics": intelligence.agent_metrics,
                "compliance_flags": intelligence.compliance_flags,
                "attributes": intelligence.extracted_attributes,
                "analyzed_at": intelligence.analyzed_at.isoformat()
                if intelligence.analyzed_at else None,
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
                "last_seen_at": device.last_seen_at.isoformat()
                if device.last_seen_at else None,
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
        call = result["call"]
        if (
            result["event_created"]
            and result["event"].event_type in TERMINAL_EVENTS
            and ((call.notes or "").strip() or (call.transcript or "").strip())
        ):
            transaction.on_commit(
                lambda: analyze_call_intelligence.delay(str(call.id))
            )
        return Response(
            {
                "ok": True,
                "event_created": result["event_created"],
                "lead_created": result["lead_created"],
                "call": serialize_call(call),
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

        source = str(request.query_params.get("source") or "").strip()
        if source in CallRecord.Source.values:
            qs = qs.filter(source=source)

        intent = str(request.query_params.get("intent") or "").strip()
        if intent in CallIntelligenceResult.Intent.values:
            qs = qs.filter(intelligence__intent=intent)

        pipeline_id = str(request.query_params.get("pipeline_id") or "").strip()
        if pipeline_id:
            qs = qs.filter(lead__pipeline_id=pipeline_id)

        agent_id = str(request.query_params.get("agent_id") or "").strip()
        if agent_id and user.role != User.Role.AGENT:
            qs = qs.filter(user_id=agent_id)

        query = str(request.query_params.get("q") or "").strip()
        if query:
            qs = qs.filter(
                Q(phone_number__icontains=query)
                | Q(contact_name__icontains=query)
                | Q(lead__name__icontains=query)
            )

        date_from = parse_date(str(request.query_params.get("date_from") or ""))
        date_to = parse_date(str(request.query_params.get("date_to") or ""))
        if date_from:
            qs = qs.filter(ended_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(ended_at__date__lte=date_to)

        rows = list(qs.order_by("-ended_at", "-created_at")[:250])
        return Response({"calls": [serialize_call(row) for row in rows]})


class CallMediaView(APIView):
    def patch(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)

        changed = []
        events = []
        if "recording_url" in request.data:
            call.recording_url = str(request.data.get("recording_url") or "").strip()[:1000]
            call.recording_status = str(
                request.data.get("recording_status") or (
                    "ready" if call.recording_url else ""
                )
            ).strip()[:32]
            changed += ["recording_url", "recording_status"]
            if call.recording_url:
                events.append(CallEvent.Type.RECORDING_READY)

        if "transcript" in request.data:
            call.transcript = str(request.data.get("transcript") or "").strip()[:100000]
            call.transcript_status = str(
                request.data.get("transcript_status") or (
                    "completed" if call.transcript else ""
                )
            ).strip()[:32]
            if isinstance(request.data.get("transcript_speakers"), list):
                call.transcript_speakers = request.data["transcript_speakers"][:1000]
                changed.append("transcript_speakers")
            changed += ["transcript", "transcript_status"]
            if call.transcript:
                events.append(CallEvent.Type.TRANSCRIPTION_COMPLETED)

        if not changed:
            return Response({"detail": "No media fields were supplied."}, status=400)

        call.save(update_fields=[*set(changed), "updated_at"])
        for event_type in events:
            CallEvent.objects.create(
                event_uuid=uuid.uuid4(),
                organization=call.organization,
                call=call,
                device=call.device,
                user=call.user,
                event_type=event_type,
                occurred_at=timezone.now(),
                payload={"source": "provider_media_update"},
            )
        if (call.notes or "").strip() or (call.transcript or "").strip():
            transaction.on_commit(
                lambda: analyze_call_intelligence.delay(str(call.id))
            )
        return Response({"ok": True, "call": serialize_call(call)})


class CallNotesView(APIView):
    def patch(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)

        notes = str(request.data.get("notes") or "").strip()[:20000]
        disposition = str(request.data.get("disposition") or "").strip()[:80]
        if disposition and not get_call_dispositions(user.organization).filter(
            code=disposition
        ).exists():
            return Response({"detail": "Choose a valid call disposition."}, status=400)

        call.notes = notes
        call.disposition = disposition
        call.save(update_fields=["notes", "disposition", "updated_at"])
        if call.crm_call_id:
            call.crm_call.notes = notes
            call.crm_call.save(update_fields=["notes"])
        if notes or call.transcript:
            transaction.on_commit(
                lambda: analyze_call_intelligence.delay(str(call.id))
            )
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
            # LeadReminder's manager enforces SHVYA's one-reminder-per-lead
            # contract and deletes the previous reminder under a Lead row lock.
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


class CallDispositionCollectionView(APIView):
    def get(self, request):
        user = _user(request)
        rows = get_call_dispositions(user.organization)
        return Response({
            "dispositions": [
                {
                    "code": row.code,
                    "name": row.name,
                    "category": row.category,
                    "position": row.position,
                    "is_active": row.is_active,
                }
                for row in rows
            ]
        })

    def post(self, request):
        user = _user(request)
        if user.role != User.Role.ADMIN:
            raise PermissionDenied("Only organization admins can edit call dispositions.")

        code = slugify(str(request.data.get("code") or request.data.get("name") or ""))[:80]
        name = str(request.data.get("name") or "").strip()[:120]
        category = str(request.data.get("category") or "").strip()
        if not code or not name:
            return Response({"detail": "Disposition name is required."}, status=400)
        if category not in CallDisposition.Category.values:
            return Response({"detail": "Choose a valid disposition category."}, status=400)

        position = request.data.get("position", 0)
        try:
            position = max(0, int(position))
        except (TypeError, ValueError):
            return Response({"detail": "Position must be a whole number."}, status=400)

        row, _ = CallDisposition.objects.update_or_create(
            organization=user.organization,
            code=code,
            defaults={
                "name": name,
                "category": category,
                "position": position,
                "is_active": bool(request.data.get("is_active", True)),
            },
        )
        return Response({
            "ok": True,
            "disposition": {
                "code": row.code,
                "name": row.name,
                "category": row.category,
                "position": row.position,
                "is_active": row.is_active,
            },
        })


class CallAnalyticsView(APIView):
    def get(self, request):
        user = _user(request)
        since = timezone.now() - timedelta(days=30)
        qs = _call_queryset(user).filter(ended_at__gte=since)
        stats = qs.aggregate(
            total=Count("id"),
            answered=Count("id", filter=Q(status=CallRecord.Status.ANSWERED)),
            missed=Count("id", filter=Q(status=CallRecord.Status.MISSED)),
            average_talk=Avg("talk_duration_seconds"),
            high_intent=Count(
                "id",
                filter=Q(intelligence__intent=CallIntelligenceResult.Intent.HIGH),
            ),
        )
        agents = (
            qs.exclude(user_id__isnull=True)
            .values("user_id", "user__name", "user__email")
            .annotate(
                calls=Count("id"),
                answered=Count("id", filter=Q(status=CallRecord.Status.ANSWERED)),
                average_talk=Avg("talk_duration_seconds"),
                high_intent=Count(
                    "id",
                    filter=Q(intelligence__intent=CallIntelligenceResult.Intent.HIGH),
                ),
            )
            .order_by("-calls", "user__name")
        )
        return Response({
            "window_days": 30,
            "stats": {
                "total": stats["total"] or 0,
                "answered": stats["answered"] or 0,
                "missed": stats["missed"] or 0,
                "average_talk": int(stats["average_talk"] or 0),
                "high_intent": stats["high_intent"] or 0,
            },
            "agents": list(agents),
        })


class CallSettingsView(APIView):
    def get(self, request):
        user = _user(request)
        settings_obj = get_call_settings(user.organization)
        release = CallAppRelease.objects.filter(is_active=True).first()
        dispositions = get_call_dispositions(user.organization)
        return Response({
            "enabled": settings_obj.enabled,
            "auto_create_answered_incoming": settings_obj.auto_create_answered_incoming,
            "auto_create_answered_outgoing": settings_obj.auto_create_answered_outgoing,
            "auto_create_missed": settings_obj.auto_create_missed,
            "auto_create_rejected": settings_obj.auto_create_rejected,
            "auto_create_unknown": settings_obj.auto_create_unknown,
            "default_pipeline_id": str(settings_obj.default_pipeline_id)
            if settings_obj.default_pipeline_id else None,
            "default_stage_id": str(settings_obj.default_stage_id)
            if settings_obj.default_stage_id else None,
            "default_owner_id": str(settings_obj.default_owner_id)
            if settings_obj.default_owner_id else None,
            "dispositions": [
                {
                    "code": row.code,
                    "name": row.name,
                    "category": row.category,
                }
                for row in dispositions
            ],
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
