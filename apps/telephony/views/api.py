import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.crm.models import AttributeDefinition, Lead, LeadReminder, Stage
from apps.crm.views.api import get_user_pipelines
from apps.organizations.access import crm_user_is_authorized
from services.crm_activity_service import record_reminder_created, record_reminder_completed
from services.crm.lead_service import create_lead
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
    move_lead_to_stage,
)

from ..models import (
    CallAppRelease,
    CallDevice,
    CallDisposition,
    CallEvent,
    CallIntelligenceResult,
    CallRecord,
)
from ..services import (
    get_call_dispositions,
    get_call_settings,
    ingest_call_event,
    register_device,
    normalize_call_phone,
    resolve_user_pipeline_stage,
    reconcile_call_tracking,
    request_call_analysis,
)
from ..analytics import call_metrics, team_metrics, format_duration


def _user(request):
    user = request.user
    if not crm_user_is_authorized(user):
        raise PermissionDenied("Active organization user is required.")
    from apps.organizations.features import module_enabled
    if not module_enabled(user.organization, "calls"):
        raise PermissionDenied("Upgrade to unlock Call Intelligence.")
    return user


def _error(exc):
    if getattr(exc, "code", None) == "device_removed":
        return Response({"code": "device_removed", "detail": "This device has been removed."}, status=403)
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
        "talk_duration": format_duration(call.talk_duration_seconds),
        "ring_duration": format_duration(call.ring_duration_seconds),
        "total_duration": format_duration(call.total_duration_seconds),
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
        "analysis_status": ("completed" if intelligence else call.analysis_status),
        "analysis_error": call.analysis_error,
        "disposition": call.disposition,
        "follow_up_required": call.follow_up_required,
        "follow_up_at": call.follow_up_at.isoformat() if call.follow_up_at else None,
        "lead": (
            {
                "id": str(call.lead_id),
                "name": call.lead.name,
                "phone": call.lead.phone,
                "email": call.lead.email,
                "pipeline": call.lead.pipeline.name,
                "pipeline_id": str(call.lead.pipeline_id),
                "stage": call.lead.stage.name,
                "stage_id": str(call.lead.stage_id),
                "source": call.lead.lead_source,
                "attributes": call.lead.attributes,
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
        call.refresh_from_db()
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
        reconcile_call_tracking(user)
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

        if request.query_params.get("mine") == "1":
            qs = qs.filter(user=user)
        if request.query_params.get("needs_follow_up") == "1":
            qs = qs.filter(follow_up_required=True)
        if request.query_params.get("unlinked") == "1":
            qs = qs.filter(lead__isnull=True)
        if request.query_params.get("linked") == "1":
            qs = qs.filter(lead__isnull=False)
        disposition = str(request.query_params.get("disposition") or "").strip()
        if disposition == "unclassified":
            qs = qs.filter(disposition="")
        elif disposition:
            qs = qs.filter(disposition=disposition)
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            return Response({"detail": "Invalid page."}, status=400)
        stats = qs.aggregate(
            total=Count("id"), incoming=Count("id", filter=Q(direction="incoming")),
            outgoing=Count("id", filter=Q(direction="outgoing")),
            missed=Count("id", filter=Q(status="missed")),
            picked=Count("id", filter=Q(status="answered")),
            total_ring=Sum("ring_duration_seconds"),
        )
        stats["not_picked"] = stats["total"] - stats["picked"]
        stats["total_ring"] = stats["total_ring"] or 0
        start = (page - 1) * 50
        rows = list(qs.order_by("-ended_at", "-created_at")[start:start + 51])
        return Response({"calls": [serialize_call(row) for row in rows[:50]],
                         "stats": stats, "page": page, "has_next": len(rows) > 50})


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
        request_call_analysis(call.id, retry_failed=True)
        call.refresh_from_db()
        return Response({"ok": True, "call": serialize_call(call)})


class CallNotesView(APIView):
    def patch(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)

        notes = str(request.data.get("notes", call.notes) or "").strip()[:20000]
        disposition = str(request.data.get("disposition", call.disposition) or "").strip()[:80]
        if disposition and not get_call_dispositions(user.organization).filter(
            code=disposition
        ).exists():
            return Response({"detail": "Choose a valid call disposition."}, status=400)

        with transaction.atomic():
            call = _call_queryset(user).select_for_update(of=("self",)).get(pk=call_id)
            call.notes = notes
            call.disposition = disposition
            call.save(update_fields=["notes", "disposition", "updated_at"])
            if call.crm_call_id:
                call.crm_call.notes = notes
                call.crm_call.save(update_fields=["notes"])
            request_call_analysis(call.id, retry_failed=True)
        call.refresh_from_db()
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
        reconcile_call_tracking(user)
        from .dashboard import _filtered_calls
        # Accept the existing mobile ID parameter names as well as web filters.
        params = request.GET.copy()
        for mobile, web in (("pipeline_id", "pipeline"), ("agent_id", "agent")):
            if params.get(mobile):
                params[web] = params[mobile]
        request.GET = params
        qs = _filtered_calls(request, user)
        if not params.get("date_from") and not params.get("date_to"):
            qs = qs.filter(ended_at__gte=timezone.now() - timedelta(days=30))
        if params.get("mine") == "1":
            qs = qs.filter(user=user)
        stats = call_metrics(qs)
        return Response({"window_days": None if params.get("date_from") or params.get("date_to") else 30, "stats": stats, "agents": team_metrics(qs)})


class CallSettingsView(APIView):
    def get(self, request):
        user = _user(request)
        settings_obj = get_call_settings(user.organization)
        release = CallAppRelease.objects.filter(is_active=True).first()
        dispositions = get_call_dispositions(user.organization)
        return Response({
            "can_edit": user.role == User.Role.ADMIN,
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


    def patch(self, request):
        user = _user(request)
        if user.role != User.Role.ADMIN:
            raise PermissionDenied("Only organization admins can change lead creation settings.")
        fields = ("auto_create_answered_incoming", "auto_create_answered_outgoing", "auto_create_missed")
        if not request.data or any(k not in fields or not isinstance(v, bool) for k, v in request.data.items()):
            return Response({"detail": "Supply boolean lead creation settings."}, status=400)
        with transaction.atomic():
            obj = get_call_settings(user.organization)
            obj = type(obj).objects.select_for_update().get(pk=obj.pk)
            for key, value in request.data.items():
                setattr(obj, key, value)
            obj.save(update_fields=[*request.data.keys(), "updated_at"])
        return self.get(request)


def _reminders(user):
    # CRM reminders belong to leads in a pipeline; the creator/assignee may differ
    # from the employee who owns that pipeline.
    return LeadReminder.objects.filter(
        lead__organization=user.organization,
        lead__pipeline__in=get_user_pipelines(user),
    ).select_related("lead")


class MobileReminderCollectionView(APIView):
    def get(self, request):
        user = _user(request)
        base = _reminders(user)
        pending = base.filter(status="pending")
        now = timezone.now()
        today = timezone.localdate()
        counts = pending.aggregate(
            total=Count("id"), overdue=Count("id", filter=Q(due_at__lt=now)),
            today=Count("id", filter=Q(due_at__gte=now, due_at__date=today)),
            upcoming=Count("id", filter=Q(due_at__date__gt=today)),
        )
        counts["completed"] = base.filter(status="completed").count()

        status_filter = str(request.query_params.get("status") or "pending").strip()
        qs = base.filter(status="completed" if status_filter == "completed" else "pending")
        segment = str(request.query_params.get("segment") or "").strip()
        if status_filter != "completed":
            if segment == "overdue":
                qs = qs.filter(due_at__lt=now)
            elif segment == "today":
                qs = qs.filter(due_at__gte=now, due_at__date=today)
            elif segment == "upcoming":
                qs = qs.filter(due_at__date__gt=today)

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            return Response({"detail": "Invalid page."}, status=400)
        start = (page - 1) * 50
        ordering = ("-completed_at", "-updated_at") if status_filter == "completed" else ("due_at", "id")
        rows = list(qs.order_by(*ordering)[start:start + 51])
        return Response({"stats": counts, "has_next": len(rows) > 50, "reminders": [{
            "id": str(row.id),
            "lead_id": str(row.lead_id),
            "lead_name": row.lead.name,
            "phone": row.lead.phone,
            "title": row.title,
            "description": row.description,
            "due_at": row.due_at.isoformat(),
            "overdue": row.status == "pending" and row.due_at < now,
            "status": row.status,
        } for row in rows[:50]]})


class MobileReminderActionView(APIView):
    def post(self, request, reminder_id):
        user = _user(request)
        action = request.data.get("action")
        if action not in {"complete", "snooze", "delete"}:
            return Response({"detail": "Invalid reminder action."}, status=400)
        with transaction.atomic():
            row = _reminders(user).select_for_update().filter(pk=reminder_id, status="pending").first()
            if row is None:
                return Response({"detail": "Reminder not found."}, status=404)
            if action == "delete":
                row.delete()
            elif action == "complete":
                row.status = "completed"
                row.completed_at = timezone.now()
                row.save(update_fields=["status", "completed_at", "updated_at"])
                record_reminder_completed(lead=row.lead, actor=user, reminder=row)
            else:
                row.due_at = max(row.due_at, timezone.now()) + timedelta(minutes=30)
                row.save(update_fields=["due_at", "updated_at"])
                row.notification_acknowledgements.all().delete()
            calls = CallRecord.objects.filter(organization=user.organization, lead=row.lead)
            if action == "snooze":
                calls.filter(follow_up_required=True).update(follow_up_at=row.due_at)
            else:
                calls.update(follow_up_required=False, follow_up_at=None)
        return Response({"ok": True})


def get_mobile_pipelines(user):
    owned = get_user_pipelines(user).filter(owner=user)
    return owned if owned.exists() else get_user_pipelines(user)


class MobileLeadCollectionView(APIView):
    def get(self, request):
        user = _user(request)
        pipelines = list(
            get_mobile_pipelines(user)
            .filter(organization=user.organization)
            .prefetch_related("stages")
        )
        selected = next((p for p in pipelines if p.owner_id == user.id), None)
        if selected is None:
            selected = next((p for p in pipelines if p.name.casefold() == "leads"), None)
        if selected is None and pipelines:
            selected = pipelines[0]
        definitions = AttributeDefinition.objects.filter(
            organization=user.organization
        ).exclude(key="booked_at").order_by("display_order", "created_at")
        return Response({
            "organization_name": user.organization.name,
            "default_pipeline_id": str(selected.id) if selected else None,
            "pipelines": [
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "country_code": pipeline.country_code,
                    "stages": [
                        {"id": str(stage.id), "name": stage.name}
                        for stage in sorted(
                            (stage for stage in pipeline.stages.all() if stage.is_active),
                            key=lambda stage: (stage.display_order, stage.name),
                        )
                    ],
                }
                for pipeline in pipelines
            ],
            "attributes": [
                {
                    "key": definition.key,
                    "name": definition.name,
                    "field_type": definition.field_type,
                    "options": definition.options if definition.field_type == "option" else [],
                }
                for definition in definitions
            ],
        })

    def post(self, request):
        user = _user(request)
        name = str(request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Lead name is required."}, status=400)
        pipeline_id = request.data.get("pipeline_id")
        stage_id = request.data.get("stage_id")
        if bool(pipeline_id) != bool(stage_id):
            return Response({"detail": "Choose a pipeline and stage."}, status=400)
        try:
            if pipeline_id:
                pipeline = get_mobile_pipelines(user).filter(
                    organization=user.organization, pk=pipeline_id
                ).first()
                if pipeline is None:
                    return Response({"detail": "Pipeline is not available."}, status=400)
                stage = Stage.objects.filter(
                    pipeline=pipeline, pk=stage_id, is_active=True
                ).first()
                if stage is None:
                    return Response({"detail": "Stage is not available."}, status=400)
            else:
                # Existing 1.1 clients only send name and phone.
                settings_obj = get_call_settings(user.organization)
                pipeline, stage = resolve_user_pipeline_stage(user, settings_obj)

            incoming_attributes = request.data.get("attributes", {})
            if not isinstance(incoming_attributes, dict):
                return Response({"detail": "Attributes must be an object."}, status=400)
            definitions = {
                item.key: item
                for item in AttributeDefinition.objects.filter(organization=user.organization).exclude(key="booked_at")
            }
            if not set(incoming_attributes).issubset(definitions):
                return Response({"detail": "Unknown lead attribute."}, status=400)
            attributes = {}
            for key, value in incoming_attributes.items():
                definition = definitions[key]
                if not isinstance(value, str):
                    return Response({"detail": f"Invalid value for {definition.name}."}, status=400)
                value = value.strip()
                if definition.field_type == "option" and value and value not in definition.options:
                    return Response({"detail": f"Invalid option for {definition.name}."}, status=400)
                attributes[key] = value

            phone = normalize_call_phone(request.data.get("phone"), pipeline=pipeline)
            # Never change an existing lead's name, owner, pipeline or stage.
            existing = Lead.objects.filter(organization=user.organization, phone=phone).exists()
            if existing:
                return Response({"detail": "This phone number is already in your CRM."}, status=409)
            lead = create_lead(organization=user.organization, pipeline=pipeline, stage=stage,
                               name=name, phone=phone,
                               email=str(request.data.get("email") or "").strip(),
                               notes=str(request.data.get("notes") or "").strip(),
                               attributes=attributes, lead_source="system", send_welcome=False)
        except DjangoValidationError as exc:
            return _error(exc)
        return Response({"ok": True, "lead_id": str(lead.id)}, status=201)


def _mobile_leads(user):
    return (
        Lead.objects
        .filter(
            organization=user.organization,
            pipeline__in=get_mobile_pipelines(user),
        )
        .select_related("pipeline", "stage")
    )


def _serialize_mobile_lead(lead):
    reminder = (
        LeadReminder.objects
        .filter(lead=lead, status="pending")
        .order_by("due_at", "id")
        .first()
    )
    last_call = (
        CallRecord.objects
        .filter(organization=lead.organization, lead=lead)
        .order_by("-ended_at", "-created_at")
        .first()
    )
    return {
        "id": str(lead.id),
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "pipeline": lead.pipeline.name,
        "pipeline_id": str(lead.pipeline_id),
        "stage": lead.stage.name,
        "stage_id": str(lead.stage_id),
        "source": lead.lead_source,
        "notes": lead.notes,
        "attributes": lead.attributes,
        "created_at": lead.created_at.isoformat(),
        "updated_at": lead.updated_at.isoformat(),
        "reminder": (
            {
                "id": str(reminder.id),
                "title": reminder.title,
                "description": reminder.description,
                "due_at": reminder.due_at.isoformat(),
                "overdue": reminder.due_at < timezone.now(),
            }
            if reminder else None
        ),
        "last_call": (
            {
                "id": str(last_call.id),
                "status": last_call.status,
                "direction": last_call.direction,
                "ended_at": last_call.ended_at.isoformat() if last_call.ended_at else None,
                "talk_duration": format_duration(last_call.talk_duration_seconds),
                "disposition": last_call.disposition,
            }
            if last_call else None
        ),
    }


class CallDetailView(APIView):
    def get(self, request, call_id):
        user = _user(request)
        call = _call_queryset(user).filter(pk=call_id).first()
        if call is None:
            return Response({"detail": "Call not found."}, status=404)
        history = _call_queryset(user)
        if call.lead_id:
            history = history.filter(lead_id=call.lead_id)
        else:
            history = history.filter(phone_number=call.phone_number)
        history = history.exclude(pk=call.pk).order_by("-ended_at", "-created_at")[:10]
        return Response({
            "call": serialize_call(call),
            "history": [serialize_call(item) for item in history],
            "dispositions": [
                {
                    "code": row.code,
                    "name": row.name,
                    "category": row.category,
                }
                for row in get_call_dispositions(user.organization)
                if row.is_active
            ],
        })


class MobileTodayView(APIView):
    def get(self, request):
        user = _user(request)
        reconcile_call_tracking(user)
        now = timezone.now()
        today = timezone.localdate()
        calls = _call_queryset(user).filter(ended_at__date=today)
        reminders = _reminders(user).filter(status="pending")
        accessible_leads = _mobile_leads(user)
        stats = calls.aggregate(
            total=Count("id"),
            answered=Count("id", filter=Q(status="answered")),
            missed=Count("id", filter=Q(status="missed")),
            outgoing=Count("id", filter=Q(direction="outgoing")),
            incoming=Count("id", filter=Q(direction="incoming")),
        )
        stats.update(
            followups_due=reminders.filter(
                due_at__gte=now, due_at__date=today
            ).count(),
            overdue=reminders.filter(due_at__lt=now).count(),
            new_leads=accessible_leads.filter(created_at__date=today).count(),
            missed_needing_action=calls.filter(
                status="missed", disposition=""
            ).count(),
        )
        recent_calls = calls.order_by("-ended_at", "-created_at")[:8]
        due = reminders.order_by("due_at", "id")[:8]
        return Response({
            "date": today.isoformat(),
            "stats": stats,
            "recent_calls": [serialize_call(item) for item in recent_calls],
            "reminders": [
                {
                    "id": str(row.id),
                    "lead_id": str(row.lead_id),
                    "lead_name": row.lead.name,
                    "phone": row.lead.phone,
                    "title": row.title,
                    "description": row.description,
                    "due_at": row.due_at.isoformat(),
                    "overdue": row.due_at < now,
                }
                for row in due
            ],
        })


class MobileLeadListView(APIView):
    def get(self, request):
        user = _user(request)
        qs = _mobile_leads(user)
        query = str(request.query_params.get("q") or "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(phone__icontains=query)
                | Q(email__icontains=query)
            )
        pipeline_id = str(request.query_params.get("pipeline_id") or "").strip()
        if pipeline_id:
            qs = qs.filter(pipeline_id=pipeline_id)
        stage_id = str(request.query_params.get("stage_id") or "").strip()
        if stage_id:
            qs = qs.filter(stage_id=stage_id)
        rows = list(qs.order_by("-updated_at")[:100])
        return Response({
            "count": qs.count(),
            "results": [_serialize_mobile_lead(lead) for lead in rows],
        })


class MobileLeadDetailView(APIView):
    def get(self, request, lead_id):
        user = _user(request)
        lead = _mobile_leads(user).filter(pk=lead_id).first()
        if lead is None:
            return Response({"detail": "Lead not found."}, status=404)
        pipelines = list(
            get_mobile_pipelines(user)
            .filter(organization=user.organization)
            .prefetch_related("stages")
        )
        recent_calls = list(
            _call_queryset(user)
            .filter(lead=lead)
            .order_by("-ended_at", "-created_at")[:15]
        )
        data = _serialize_mobile_lead(lead)
        data["pipelines"] = [
            {
                "id": str(pipeline.id),
                "name": pipeline.name,
                "stages": [
                    {"id": str(stage.id), "name": stage.name}
                    for stage in sorted(
                        (stage for stage in pipeline.stages.all() if stage.is_active),
                        key=lambda stage: (stage.display_order, stage.name),
                    )
                ],
            }
            for pipeline in pipelines
        ]
        data["recent_calls"] = [serialize_call(call) for call in recent_calls]
        return Response({"lead": data})

    def patch(self, request, lead_id):
        user = _user(request)
        lead = _mobile_leads(user).filter(pk=lead_id).first()
        if lead is None:
            return Response({"detail": "Lead not found."}, status=404)

        pipeline_id = str(request.data.get("pipeline_id") or lead.pipeline_id)
        stage_id = str(request.data.get("stage_id") or "").strip()
        if not stage_id:
            return Response({"detail": "stage_id is required."}, status=400)
        pipeline = get_mobile_pipelines(user).filter(
            organization=user.organization,
            pk=pipeline_id,
        ).first()
        if pipeline is None:
            return Response({"detail": "Pipeline is not available."}, status=400)
        stage = Stage.objects.filter(
            pipeline=pipeline,
            pk=stage_id,
            is_active=True,
        ).first()
        if stage is None:
            return Response({"detail": "Stage is not available."}, status=400)

        try:
            if lead.pipeline_id == pipeline.id:
                move_lead_to_stage(lead=lead, stage=stage, actor=user)
            else:
                move_lead_to_pipeline_stage(
                    lead=lead,
                    pipeline=pipeline,
                    stage=stage,
                    actor=user,
                )
        except LeadTransitionError as exc:
            return Response({"detail": str(exc)}, status=400)

        lead.refresh_from_db()
        return Response({"ok": True, "lead": _serialize_mobile_lead(lead)})
