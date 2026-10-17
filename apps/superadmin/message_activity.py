"""Read-only organisation messaging activity over canonical domain records.

The feed is an ordered SQL UNION of small record identities. Message delivery,
AI execution, queues, and Cadence progress remain separate records and states.
Hydration is batched; exports never load the entire conversation corpus. Current
CRM pipeline/owner filters do not claim to reconstruct historic assignments.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from itertools import islice
import math
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Subquery, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Concat, Replace
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai_engagement.models import AITrace
from apps.ai_engagement.services.trace_sanitizer import redact_text, safe_error_message
from apps.channels.campaign_models import CampaignAttempt
from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.instagram_models import InstagramAccount, InstagramMessage
from apps.channels.models import AIMessageSendState, BulkMessageRecipient, WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.followups.models import FollowupExecution, FollowupSequence, LeadSequenceState
from apps.hosted_automation.models import HostedAutomationJob
from apps.triggers.models import TriggerRun
from services.channels.hosted_whatsapp_service import normalize_whatsapp_number


KINDS = {
    "whatsapp": "WhatsApp message", "instagram": "Instagram message",
    "ai": "AI execution", "queue": "AI queue", "cadence": "Cadence attempt",
    "sequence": "Current Cadence progress", "workflow": "Workflow execution",
}
OUTCOMES = {"attention": "Needs attention", "waiting": "Waiting / processing", "complete": "Completed / resolved"}
DIRECTIONS = {"inbound": "Inbound", "outbound": "Outbound", "internal": "Internal activity"}
WAITING = {"queued", "processing", "started", "pending", "scheduled", "retry_wait", "retrying", "active", "paused", "sending", "dispatching", "email_ready"}
COMPLETE = {"sent", "delivered", "read", "received", "accepted", "completed", "created", "skipped", "duplicate", "silenced", "inactive", "cleared"}
_SOURCES = {
    "whatsapp": "whatsapp", "instagram": "instagram", "ai": "ai",
    "hosted_queue": "queue", "wa_queue": "queue", "ig_queue": "queue",
    "cadence": "cadence", "sequence": "sequence", "workflow": "workflow",
}
_QUEUE_PREFIXES = {"hosted_queue": "hosted", "wa_queue": "wa", "ig_queue": "ig"}
_CHUNK = 200
_DETAIL_LIMIT = 100


@dataclass(frozen=True)
class ActivityFilters:
    organization_id: str
    values: dict
    tz: ZoneInfo
    from_date: date
    to_date: date
    start: datetime
    end: datetime


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return redact_text(value) if isinstance(value, (str, int, float)) else ""


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed and timezone.is_aware(parsed) else None


def _zone(organization):
    try:
        return ZoneInfo(organization.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _valid_lead(organization, prefix="lead", nullable=False):
    path = f"{prefix}__" if prefix else ""
    condition = Q(**{
        f"{path}organization_id": organization.pk,
        f"{path}is_operations_test": False,
        f"{path}pipeline__organization_id": organization.pk,
        f"{path}stage__pipeline_id": F(f"{path}pipeline_id"),
    })
    return condition | Q(**{f"{prefix}__isnull": True}) if nullable else condition


def _wa_base(organization):
    return WhatsAppMessage.objects.filter(
        organization=organization, account__organization=organization,
        account__is_operations_test=False,
    ).filter(_valid_lead(organization, nullable=True))


def _ig_base(organization):
    return InstagramMessage.objects.filter(
        organization=organization, account__organization=organization,
        conversation__organization=organization, conversation__account_id=F("account_id"),
    ).filter(_valid_lead(organization, "conversation__lead", nullable=True))


def _sequence_scope(organization, prefix="sequence"):
    path = f"{prefix}__" if prefix else ""
    return Q(**{f"{path}organization_id": organization.pk}) & (
        Q(**{f"{path}whatsapp_account__isnull": True})
        | Q(**{f"{path}whatsapp_account__organization_id": organization.pk,
               f"{path}whatsapp_account__is_operations_test": False})
    ) & (
        Q(**{f"{path}instagram_account__isnull": True})
        | Q(**{f"{path}instagram_account__organization_id": organization.pk})
    )


def _execution_base(organization):
    return FollowupExecution.objects.filter(
        organization=organization, state__organization=organization,
        state__lead_id=F("lead_id"), state__sequence_id=F("sequence_id"),
        step__sequence_id=F("sequence_id"),
    ).filter(_valid_lead(organization), _sequence_scope(organization)).filter(
        Q(whatsapp_message__isnull=True) | Q(
            whatsapp_message__organization=organization,
            whatsapp_message__account__organization=organization,
            whatsapp_message__account__is_operations_test=False,
            whatsapp_message__lead_id=F("lead_id"),
            whatsapp_message__direction="outbound",
        )
    )


def _workflow_base(organization):
    return TriggerRun.objects.filter(
        rule__organization=organization, event__organization=organization,
        event__lead_id=F("lead_id"),
    ).filter(_valid_lead(organization)).filter(
        Q(message__isnull=True) | Q(
            message__organization=organization, message__lead_id=F("lead_id"),
            message__account__organization=organization, message__account__is_operations_test=False,
            message__direction="outbound",
        )
    )


def _source_base(organization, source):
    if source in {"whatsapp", "wa_queue"}:
        rows = _wa_base(organization)
        if source == "wa_queue":
            rows = rows.filter(direction="inbound", account__connection_type="api", lead__isnull=False,
                               raw_payload__has_key="shvya_ai_execution")
        return rows
    if source in {"instagram", "ig_queue"}:
        rows = _ig_base(organization)
        if source == "ig_queue":
            rows = rows.filter(direction="inbound", conversation__lead__isnull=False,
                               raw_payload__has_key="shvya_ai_execution")
        return rows
    if source == "ai":
        return AITrace.objects.filter(organization=organization).exclude(connection_type="sandbox").filter(_valid_lead(organization))
    if source == "hosted_queue":
        return HostedAutomationJob.objects.filter(
            organization=organization, account__organization=organization,
            account__is_operations_test=False, account__connection_type="hosted",
        ).filter(_valid_lead(organization)).filter(
            Q(source_message__isnull=True) | Q(
                source_message__organization=organization, source_message__account_id=F("account_id"),
                source_message__lead_id=F("lead_id"), source_message__direction="inbound",
            )
        )
    if source == "cadence":
        return _execution_base(organization)
    if source == "sequence":
        return LeadSequenceState.objects.filter(
            organization=organization, status__in=["active", "paused"],
        ).filter(_valid_lead(organization), _sequence_scope(organization)).filter(
            Q(next_step__isnull=True) | Q(next_step__sequence_id=F("sequence_id"))
        )
    if source == "workflow":
        return _workflow_base(organization)
    raise ValueError("Unsupported activity type.")


def parse_filters(organization, params):
    """Validate every filter against this explicit Superadmin tenant context."""
    tz = _zone(organization)
    today = timezone.localdate(timezone=tz)
    try:
        first = date.fromisoformat(str(params.get("from") or (today - timedelta(days=6)).isoformat()))
        last = date.fromisoformat(str(params.get("to") or today.isoformat()))
    except (ValueError, TypeError):
        raise ValueError("Choose valid start and end dates.") from None
    if first > last or last == date.max or (last - first).days > 366:
        raise ValueError("Choose a date range of up to 367 days, with the start before the end.")
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
    try:
        # Positive offsets on year 1 (or negative offsets at the upper bound)
        # can exceed Python's UTC datetime range during database adaptation.
        start.astimezone(datetime_timezone.utc)
        end.astimezone(datetime_timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("Choose dates that can be represented in this organisation's timezone.") from None
    values = {key: str(params.get(key) or "").strip() for key in (
        "pipeline", "connection", "owner", "sequence", "kind", "q", "outcome", "direction",
    )}
    for key, choices in (("kind", KINDS), ("outcome", OUTCOMES), ("direction", DIRECTIONS)):
        if values[key] == "all":
            values[key] = ""
        if values[key] and values[key] not in choices:
            raise ValueError(f"Choose a valid {key} filter.")
    if len(values["q"]) > 200:
        raise ValueError("Search text must be at most 200 characters.")
    if "\x00" in values["q"]:
        raise ValueError("Search text contains an unsupported control character.")
    selectors = {
        "pipeline": Pipeline.objects.filter(organization=organization),
        "owner": get_user_model().objects.filter(organization=organization),
        "sequence": FollowupSequence.objects.filter(_sequence_scope(organization, "")),
    }
    for key, rows in selectors.items():
        if values[key]:
            identifier = _uuid(values[key])
            if not identifier or not rows.filter(pk=identifier).exists():
                raise ValueError(f"The selected {key} is unavailable in this organisation.")
            values[key] = identifier
    if values["connection"]:
        family, separator, identifier = values["connection"].partition(":")
        identifier = _uuid(identifier)
        if not separator or family not in {"wa", "ig"} or not identifier:
            raise ValueError("Choose a valid WhatsApp or Instagram connection.")
        rows = (WhatsAppAccount.objects.filter(organization=organization, is_operations_test=False)
                if family == "wa" else InstagramAccount.objects.filter(organization=organization))
        if not rows.filter(pk=identifier).exists():
            raise ValueError("The selected connection is unavailable in this organisation.")
        values["connection"] = f"{family}:{identifier}"
    values.update({"from": first.isoformat(), "to": last.isoformat()})
    return ActivityFilters(
        str(organization.pk), values, tz, first, last, start, end,
    )


def _wa_accounts(organization, identifiers=None):
    rows = WhatsAppAccount.objects.filter(organization=organization, is_operations_test=False)
    if identifiers is not None:
        rows = rows.filter(pk__in=identifiers)
    latest_attempt = WhatsAppConnectionAttempt.objects.filter(
        organization=organization, account_id=OuterRef("pk"), status="connected",
    ).order_by("-updated_at", "-created_at", "-pk")
    coex = WhatsAppMessage.objects.filter(
        organization=organization, account_id=OuterRef("pk"), media_payload__coexistence_sync=True,
    )
    return rows.only(
        "id", "organization_id", "is_operations_test", "connection_type", "display_phone_number",
        "phone_number_id", "business_name", "is_active", "status", "updated_at",
    ).annotate(_connection_stage=Subquery(latest_attempt.values("stage")[:1]), _coexistence=Exists(coex))


def _account_label(account):
    if isinstance(account, InstagramAccount):
        return "Instagram", f"@{account.username or account.ig_user_id}"
    if account.connection_type == "hosted":
        channel = "Hosted WhatsApp"
    elif account._connection_stage in {"coexistence_connected", "coexistence_sync_warning"} or account._coexistence:
        channel = "WhatsApp Coexistence"
    else:
        channel = "WhatsApp API"
    # A Meta Graph phone_number_id is not a telephone number.
    number = account.display_phone_number or (account.phone_number_id if account.connection_type == "hosted" else "")
    return channel, normalize_whatsapp_number(phone_number=number) or number


def filter_options(organization):
    pipelines = list(Pipeline.objects.filter(organization=organization).order_by("name", "id").values(
        "id", "name", "country_code", "phone_number",
    ))
    owners = get_user_model().objects.filter(
        organization=organization, owned_pipelines__organization=organization,
    ).order_by("name", "id").values("id", "name").distinct()
    sequences = FollowupSequence.objects.filter(_sequence_scope(organization, "")).order_by("name", "id").values("id", "name")
    accounts = list(_wa_accounts(organization).order_by("business_name", "id"))
    pacing = {
        str(item["account_id"]): item
        for item in AIMessageSendState.objects.filter(
            account__organization=organization, account__is_operations_test=False,
        ).values("account_id", "next_send_at", "last_sent_at", "claimed_until")
    }
    connections = []
    for account in accounts:
        channel, number = _account_label(account)
        linked = [p["name"] for p in pipelines if number and number == normalize_whatsapp_number(
            country_code=p["country_code"], phone_number=p["phone_number"],
        )]
        label = " · ".join(filter(None, (channel, number or account.business_name or "Number unavailable", ", ".join(linked))))
        if not account.is_active or account.status != "connected":
            label += " · inactive connection"
        connections.append({"value": f"wa:{account.pk}", "label": label, "pacing": pacing.get(str(account.pk), {})})
    for account in InstagramAccount.objects.filter(organization=organization).only("id", "username", "ig_user_id", "status"):
        channel, handle = _account_label(account)
        connections.append({"value": f"ig:{account.pk}", "label": f"{channel} · {handle}"})
    return {
        "pipelines": [{"value": str(p["id"]), "label": p["name"]} for p in pipelines],
        "connections": connections,
        "owners": [{"value": str(owner["id"]), "label": owner["name"]} for owner in owners],
        "sequences": [{"value": str(item["id"]), "label": item["name"]} for item in sequences],
        "kinds": [{"value": key, "label": label} for key, label in KINDS.items()],
        "outcomes": [{"value": key, "label": label} for key, label in OUTCOMES.items()],
        "directions": [{"value": key, "label": label} for key, label in DIRECTIONS.items()],
    }


def _bare_uuid(expression):
    return Replace(Cast(expression, CharField()), Value("-"), Value(""))


def _annotate_source(rows, source):
    event_time = F("created_at")
    status = F("status")
    direction = Value("internal", output_field=CharField())
    if source in {"whatsapp", "instagram"}:
        direction = F("direction")
    elif source in {"wa_queue", "ig_queue"}:
        status = KeyTextTransform("status", "raw_payload__shvya_ai_execution")
    elif source == "ai":
        event_time = F("started_at")
    elif source == "sequence":
        event_time = F("updated_at")
    elif source == "cadence":
        direction = Case(When(step__step_type="reminder", then=Value("internal")), default=Value("outbound"), output_field=CharField())
    elif source == "workflow":
        direction = Case(When(action_type__in=["message", "email"], then=Value("outbound")), default=Value("internal"), output_field=CharField())
    rows = rows.annotate(_activity_time=event_time, _activity_status=status, _activity_direction=direction)
    return rows.annotate(_activity_outcome=Case(
        When(_activity_status__in=WAITING, then=Value("waiting")),
        When(_activity_status__in=COMPLETE, then=Value("complete")),
        default=Value("attention"), output_field=CharField(),
    ))


def _apply_connection(rows, organization, source, connection):
    family, identifier = connection.split(":", 1)
    if source in {"whatsapp", "wa_queue", "hosted_queue"}:
        return rows.filter(account_id=identifier) if family == "wa" else rows.none()
    if source in {"instagram", "ig_queue"}:
        return rows.filter(account_id=identifier) if family == "ig" else rows.none()
    if source == "ai":
        if family == "wa":
            return rows.exclude(connection_type="instagram").filter(whatsapp_account_id=identifier)
        sources = _ig_base(organization).filter(account_id=identifier, direction="inbound")
        return rows.filter(connection_type="instagram", source_inbound_message_id__in=sources.values("pk"))
    if source == "sequence":
        field = "sequence__whatsapp_account_id" if family == "wa" else "sequence__instagram_account_id"
        return rows.filter(**{field: identifier})
    if source == "cadence":
        if family == "wa":
            return rows.exclude(step__step_type__in=["email", "reminder", "instagram"]).filter(
                Q(whatsapp_message__account_id=identifier)
                | Q(whatsapp_message__isnull=True, sequence__whatsapp_account_id=identifier)
            )
        messages = _ig_base(organization).filter(
            direction="outbound", conversation__lead_id=OuterRef("lead_id"),
        ).annotate(_message_ref=_bare_uuid(F("pk"))).filter(
            _message_ref=_bare_uuid(KeyTextTransform("instagram_message_id", OuterRef("payload"))),
        ).order_by()
        return rows.filter(step__step_type="instagram").annotate(
            _saved_instagram_account=Subquery(messages.values("account_id")[:1]),
        ).filter(
            Q(_saved_instagram_account=identifier)
            | Q(_saved_instagram_account__isnull=True, sequence__instagram_account_id=identifier)
        )
    if source == "workflow" and family == "wa":
        return rows.filter(action_type="message").filter(
            Q(message__account_id=identifier) | Q(message__isnull=True, action__account=identifier)
        )
    return rows.none()


def _filtered_sources(organization, filters):
    if not isinstance(filters, ActivityFilters) or filters.organization_id != str(organization.pk):
        raise ValueError("Activity filters belong to a different organisation.")
    values = filters.values
    for source, kind in _SOURCES.items():
        if values["kind"] and values["kind"] != kind:
            continue
        rows = _annotate_source(_source_base(organization, source), source)
        rows = rows.filter(_activity_time__gte=filters.start, _activity_time__lt=filters.end)
        lead_path = "conversation__lead" if source in {"instagram", "ig_queue"} else "lead"
        if values["pipeline"]:
            rows = rows.filter(**{f"{lead_path}__pipeline_id": values["pipeline"]})
        if values["owner"]:
            rows = rows.filter(**{f"{lead_path}__pipeline__owner_id": values["owner"],
                                  f"{lead_path}__pipeline__owner__organization_id": organization.pk})
        if values["connection"]:
            rows = _apply_connection(rows, organization, source, values["connection"])
        if values["sequence"]:
            if source in {"cadence", "sequence"}:
                rows = rows.filter(sequence_id=values["sequence"])
            elif source == "whatsapp":
                linked = _execution_base(organization).filter(sequence_id=values["sequence"],
                                                             whatsapp_message_id=OuterRef("pk"), lead_id=OuterRef("lead_id"))
                rows = rows.filter(Exists(linked))
            elif source == "instagram":
                rows = rows.filter(direction="outbound")
                linked = _execution_base(organization).filter(
                    sequence_id=values["sequence"], lead_id=OuterRef("conversation__lead_id"),
                    step__step_type="instagram",
                ).annotate(_message_ref=_bare_uuid(KeyTextTransform("instagram_message_id", "payload"))).filter(
                    _message_ref=_bare_uuid(OuterRef("pk")),
                )
                rows = rows.filter(Exists(linked))
            else:
                continue
        if values["outcome"]:
            rows = rows.filter(_activity_outcome=values["outcome"])
        if values["direction"]:
            rows = rows.filter(_activity_direction=values["direction"])
        if values["q"]:
            term = values["q"]
            condition = Q(**{f"{lead_path}__name__icontains": term}) | Q(**{f"{lead_path}__phone__icontains": term}) | Q(**{f"{lead_path}__email__icontains": term})
            fields = {
                "whatsapp": ("body", "error", "external_id", "from_number", "to_number"),
                "instagram": ("body", "error", "external_id", "conversation__participant_username"),
                "wa_queue": ("body", "raw_payload__shvya_ai_execution__reason"),
                "ig_queue": ("body", "raw_payload__shvya_ai_execution__reason"),
                "ai": ("incoming_message_preview", "response_preview", "reason_code", "model_name"),
                "hosted_queue": ("source_message__body", "error", "kind"),
                "cadence": ("sequence__name", "step__title", "error"),
                "sequence": ("sequence__name", "next_step__title"),
                "workflow": ("rule__name", "action_type", "detail"),
            }[source]
            for field in fields:
                condition |= Q(**{f"{field}__icontains": term})
            rows = rows.filter(condition)
        yield source, kind, rows.order_by()


def _index_part(source, kind, rows):
    identifier = Cast("pk", CharField())
    # Cast(UUID) differs on SQLite; public IDs and ordering use one representation
    # within the active database, and the hydration boundary accepts both forms.
    if source in _QUEUE_PREFIXES:
        identifier = Concat(Value(f"{_QUEUE_PREFIXES[source]}:"), identifier, output_field=CharField())
    return rows.annotate(
        activity_time=F("_activity_time"), activity_id=identifier,
        activity_kind=Value(kind, output_field=CharField()),
        activity_source=Value(source, output_field=CharField()),
    ).values("activity_time", "activity_kind", "activity_id", "activity_source").order_by()


def _index(organization, filters):
    parts = [_index_part(source, kind, rows) for source, kind, rows in _filtered_sources(organization, filters)]
    if not parts:
        return _index_part("whatsapp", "whatsapp", _annotate_source(_wa_base(organization).none(), "whatsapp"))
    combined = parts[0].union(*parts[1:], all=True) if len(parts) > 1 else parts[0]
    return combined.order_by("-activity_time", "-activity_kind", "-activity_id", "-activity_source")


def _select_related(rows, source):
    prefix = "conversation__lead" if source in {"instagram", "ig_queue"} else "lead"
    relations = [f"{prefix}__pipeline__owner", f"{prefix}__stage"]
    if source in {"cadence", "sequence"}:
        relations += ["sequence", "step" if source == "cadence" else "next_step"]
    if source == "workflow":
        relations += ["rule", "event"]
    return rows.select_related(*relations).defer(
        f"{prefix}__notes", f"{prefix}__attributes", f"{prefix}__pipeline__owner__password",
    )


def _object_lead(item):
    return item.conversation.lead if isinstance(item, InstagramMessage) else item.lead


class _Batch:
    """Tenant-scoped relation caches; at most a small multiple of one page."""

    def __init__(self, organization, objects):
        self.organization = organization
        self.objects = objects
        wa_ids, ig_ids, wa_accounts, ig_accounts, pipelines, stages = set(), set(), set(), set(), set(), set()
        for source, records in objects.items():
            for item in records.values():
                if source in {"whatsapp", "wa_queue"}:
                    wa_ids.add(item.pk)
                    wa_ids.add(_uuid(_mapping(_mapping(item.raw_payload).get("shvya_ai")).get("source_inbound_message_id")))
                elif source in {"instagram", "ig_queue"}:
                    ig_ids.add(item.pk)
                    ig_ids.add(_uuid(_mapping(_mapping(item.raw_payload).get("shvya_ai")).get("source_inbound_message_id")))
                elif source == "ai":
                    target = ig_ids if item.connection_type == "instagram" else wa_ids
                    target.update(filter(None, (item.source_inbound_message_id, item.outbound_message_id)))
                    wa_accounts.add(item.whatsapp_account_id)
                    pipelines.add(item.pipeline_id)
                    stages.add(item.stage_id)
                elif source == "hosted_queue":
                    wa_ids.update(filter(None, (item.source_message_id, _uuid(_mapping(item.result).get("message_id")))))
                    wa_accounts.add(item.account_id)
                elif source in {"cadence", "sequence"}:
                    wa_accounts.add(item.sequence.whatsapp_account_id)
                    ig_accounts.add(item.sequence.instagram_account_id)
                    if source == "cadence":
                        wa_ids.add(item.whatsapp_message_id)
                        ig_ids.add(_uuid(_mapping(item.payload).get("instagram_message_id")))
                elif source == "workflow":
                    wa_ids.add(item.message_id)
                    if item.action_type == "message":
                        wa_accounts.add(_uuid(_mapping(item.action).get("account")))
        self.wa = {str(item.pk): item for item in _select_related(_wa_base(organization).filter(pk__in=list(filter(None, wa_ids))), "whatsapp")}
        self.ig = {str(item.pk): item for item in _select_related(_ig_base(organization).filter(pk__in=list(filter(None, ig_ids))), "instagram")}
        wa_accounts.update(item.account_id for item in self.wa.values())
        ig_accounts.update(item.account_id for item in self.ig.values())
        self.wa_accounts = {str(item.pk): item for item in _wa_accounts(organization, list(filter(None, wa_accounts)))}
        self.ig_accounts = {str(item.pk): item for item in InstagramAccount.objects.filter(
            organization=organization, pk__in=list(filter(None, ig_accounts)),
        ).only("id", "organization_id", "username", "ig_user_id", "status")}
        self.pipelines = {str(item.pk): item for item in Pipeline.objects.filter(organization=organization, pk__in=list(filter(None, pipelines)))}
        self.stages = {str(item.pk): item for item in Stage.objects.filter(pipeline__organization=organization, pk__in=list(filter(None, stages)))}
        self.pacing = {str(item["account_id"]): item for item in AIMessageSendState.objects.filter(
            account__organization=organization, account_id__in=list(filter(None, wa_accounts)),
            account__is_operations_test=False,
        ).values("account_id", "next_send_at", "last_sent_at", "claimed_until")}
        self.executions = {}
        self.workflows = {}
        # Iterate all exact associations, retaining only the latest summary per
        # message. Every attempt remains a separate, untruncated feed record.
        links = _execution_base(organization).filter(
            Q(whatsapp_message_id__in=list(self.wa))
            | Q(payload__instagram_message_id__in=list(self.ig))
        ).select_related("sequence", "step").order_by("-created_at", "-pk")
        for execution in links.iterator(chunk_size=_CHUNK):
            if execution.whatsapp_message_id:
                key = ("whatsapp", str(execution.whatsapp_message_id))
            else:
                key = ("instagram", _uuid(_mapping(execution.payload).get("instagram_message_id")))
                message = self.ig.get(key[1])
                if (not message or message.direction != "outbound"
                        or execution.step.step_type != "instagram"
                        or message.conversation.lead_id != execution.lead_id):
                    continue
            self.executions.setdefault(key, execution)
        links = _workflow_base(organization).filter(message_id__in=list(self.wa)).select_related("rule").order_by("-created_at", "-pk")
        for run in links.iterator(chunk_size=_CHUNK):
            self.workflows.setdefault(str(run.message_id), run)
        self.traces = defaultdict(list)
        self.trace_counts = defaultdict(int)
        message_ids = set(self.wa) | set(self.ig)
        traces = _source_base(organization, "ai").filter(
            Q(source_inbound_message_id__in=message_ids) | Q(outbound_message_id__in=message_ids),
        ).only(
            "id", "organization_id", "lead_id", "whatsapp_account_id", "connection_type",
            "source_inbound_message_id", "outbound_message_id", "status", "model_name", "started_at",
        ).order_by("-started_at", "-pk")
        for trace in traces.iterator(chunk_size=_CHUNK):
            instagram = trace.connection_type == "instagram"
            if not instagram and not trace.whatsapp_account_id:
                # A loose same-lead/time association is not a sender identity.
                continue
            for identifier, direction in ((trace.source_inbound_message_id, "inbound"), (trace.outbound_message_id, "outbound")):
                message = self.message(identifier, instagram=instagram, lead_id=trace.lead_id,
                                       account_id=None if instagram else trace.whatsapp_account_id)
                if message is None or message.direction != direction:
                    continue
                key = ("instagram" if instagram else "whatsapp", str(message.pk))
                self.trace_counts[key] += 1
                if len(self.traces[key]) < _DETAIL_LIMIT:
                    self.traces[key].append(trace)
        campaign_rows = CampaignAttempt.objects.filter(
            message_id__in=list(self.wa), message__organization=organization,
            message__direction="outbound", delivery__campaign__organization=organization,
            delivery__campaign__account_id=F("message__account_id"),
        ).filter(
            Q(delivery__lead__isnull=True) | Q(delivery__lead_id=F("message__lead_id")),
        ).filter(
            Q(delivery__campaign__pipeline__isnull=True)
            | Q(delivery__campaign__pipeline__organization=organization),
        ).values(
            "message_id", "delivery__campaign__name", "number", "started_at", "finished_at",
            "accepted_at", "sent_at", "delivered_at", "read_at", "failed_at", "uncertain", "error_message",
        )
        self.campaigns = {str(item["message_id"]): item for item in campaign_rows}
        legacy_rows = BulkMessageRecipient.objects.filter(
            message_id__in=list(self.wa), message__organization=organization, message__direction="outbound",
            campaign__organization=organization, campaign__account_id=F("message__account_id"),
        ).filter(
            Q(lead__isnull=True) | Q(lead_id=F("message__lead_id")),
        ).filter(
            Q(campaign__pipeline__isnull=True) | Q(campaign__pipeline__organization=organization),
        ).order_by("-created_at", "-pk").values("message_id", "campaign__name")
        self.legacy_campaigns = {}
        for item in legacy_rows.iterator(chunk_size=_CHUNK):
            self.legacy_campaigns.setdefault(str(item["message_id"]), item["campaign__name"])

    def message(self, identifier, *, instagram=False, lead_id=None, account_id=None):
        message = (self.ig if instagram else self.wa).get(_uuid(identifier))
        if message is None:
            return None
        actual_lead = message.conversation.lead_id if instagram else message.lead_id
        if lead_id is not None and actual_lead != lead_id:
            return None
        if account_id is not None and message.account_id != account_id:
            return None
        return message


def _empty_row(kind, identifier, event_time):
    row = {key: "" for key in (
        "lead_id", "lead_name", "lead_phone", "lead_email", "pipeline_id", "pipeline_name", "stage_name", "owner_name",
        "channel", "connection_id", "connection_label", "from_number", "to_number", "direction", "origin", "status", "status_label",
        "outcome", "sequence_name", "campaign_name", "content", "message_type", "error", "attempt", "external_id", "model", "tokens",
    )}
    row.update({key: None for key in ("updated_at", "sent_at", "scheduled_at", "started_at", "completed_at")})
    row.update(id=identifier, kind=kind, kind_label=KINDS[kind], event_time=event_time,
               content_is_preview=False, history=[], related=[])
    return row


def _history(row, label, stamp=None, detail=""):
    row["history"].append({"label": label, "time": stamp, "detail": _text(detail)})


def _related(row, kind, item, label=None, prefix=""):
    if item is None:
        return
    identifier = f"{prefix}{item.pk}"
    if row["kind"] == kind and row["id"] == identifier:
        return
    if not any(link["kind"] == kind and link["id"] == identifier for link in row["related"]):
        row["related"].append({"kind": kind, "id": identifier, "label": label or KINDS[kind]})


def _status(row, status):
    status = status if isinstance(status, str) and len(status) <= 64 else "unknown"
    row["status"] = _text(status) or "unknown"
    row["status_label"] = row["status"].replace("_", " ").capitalize()
    row["outcome"] = "waiting" if status in WAITING else "complete" if status in COMPLETE else "attention"


def _lead(row, lead, batch):
    if lead is None:
        return
    row.update(lead_id=str(lead.pk), lead_name=_text(lead.name), lead_phone=_text(lead.phone), lead_email=_text(lead.email),
               pipeline_id=str(lead.pipeline_id), pipeline_name=_text(lead.pipeline.name), stage_name=_text(lead.stage.name))
    owner = lead.pipeline.owner
    if owner is not None and owner.organization_id == batch.organization.pk:
        row["owner_name"] = _text(owner.name)


def _account(row, batch, identifier, *, instagram=False):
    account = (batch.ig_accounts if instagram else batch.wa_accounts).get(str(identifier or ""))
    if account is None:
        return
    channel, number = _account_label(account)
    row.update(channel=channel, connection_id=f"{'ig' if instagram else 'wa'}:{account.pk}",
               connection_label=" · ".join(filter(None, (channel, number))))
    if not instagram:
        pacing = batch.pacing.get(str(account.pk), {})
        if pacing.get("next_send_at"):
            _history(row, "Current sender pacing", pacing["next_send_at"], "Next recorded sender slot; this is current pacing, not a delivery timestamp.")


def _message_row(row, message, batch, *, instagram=False):
    lead = message.conversation.lead if instagram else message.lead
    _lead(row, lead, batch)
    _account(row, batch, message.account_id, instagram=instagram)
    row.update(direction=message.direction, content=_text(message.body), updated_at=message.updated_at,
               sent_at=message.sent_at, external_id=_text(message.external_id), error=safe_error_message(message.error),
               message_type=_text(message.message_type))
    if not instagram and _mapping(message.media_payload).get("transport") == "template":
        row["message_type"] = "template"
    _status(row, message.status)
    # Preserve persisted transport identities. Current display numbers/handles
    # belong in connection_label; changing an account cannot rewrite history.
    row.update(from_number=_text(message.sender_id if instagram else message.from_number),
               to_number=_text(message.recipient_id if instagram else message.to_number))
    payload = _mapping(message.raw_payload)
    ai = _mapping(payload.get("shvya_ai"))
    origin = "Inbound" if message.direction == "inbound" else "Other / unclassified outbound"
    execution = batch.executions.get(("instagram" if instagram else "whatsapp", str(message.pk)))
    workflow = batch.workflows.get(str(message.pk)) if not instagram else None
    trace_key = ("instagram" if instagram else "whatsapp", str(message.pk))
    traces = batch.traces.get(trace_key, [])
    campaign = batch.campaigns.get(str(message.pk)) if not instagram else None
    campaign_name = (campaign["delivery__campaign__name"] if campaign else batch.legacy_campaigns.get(str(message.pk), "")) if not instagram else ""
    if message.direction == "outbound":
        if execution:
            origin = "Cadence"
            row["sequence_name"] = _text(execution.sequence.name)
            _related(row, "cadence", execution, "Cadence attempt")
        elif workflow:
            origin = "Workflow"
            _related(row, "workflow", workflow, _text(workflow.rule.name))
        elif campaign_name:
            origin = "Campaign"
        elif payload.get("shvya_welcome"):
            origin = "Welcome"
        elif ai or traces:
            origin = "AI bump-up" if ai.get("origin") == "bump_up" else "AI reply"
        elif payload.get("shvya_auto_followup") or payload.get("shvya_followup"):
            origin = "Cadence (linked attempt unavailable)"
    row["origin"] = origin
    row["campaign_name"] = _text(campaign_name)
    for trace in traces:
        _related(row, "ai", trace, "AI execution · " + _text(trace.status))
    if traces:
        row["model"] = _text(traces[0].model_name)
    if batch.trace_counts.get(trace_key, 0) > _DETAIL_LIMIT:
        _history(row, "AI execution link coverage", detail=f"Showing the latest {_DETAIL_LIMIT} of {batch.trace_counts[trace_key]} exact related AI executions. All executions remain available in the activity feed.")
    if campaign_name:
        _history(row, "Campaign", detail=campaign_name)
    if campaign:
        row["attempt"] = campaign["number"]
        for label, field in (
            ("Campaign attempt started", "started_at"), ("Campaign provider accepted", "accepted_at"),
            ("Campaign provider sent", "sent_at"), ("Campaign delivered receipt", "delivered_at"),
            ("Campaign read receipt", "read_at"), ("Campaign failed receipt", "failed_at"),
        ):
            if campaign[field]:
                _history(row, label, campaign[field])
        if campaign["uncertain"]:
            _history(row, "Campaign outcome requires review", campaign["finished_at"], "This attempt records an uncertain provider outcome; a completed task does not establish delivery.")
        if not row["error"] and campaign["error_message"]:
            row["error"] = safe_error_message(campaign["error_message"])
    delivery = _mapping(payload.get("shvya_ai_delivery"))
    if message.direction == "outbound" and message.status in {"queued", "sending"}:
        row["scheduled_at"] = _stamp(delivery.get("available_at"))
        reason = safe_error_message(_text(delivery.get("reason")))
        if row["scheduled_at"] or reason:
            _history(row, "Queued outbound availability", row["scheduled_at"], reason or "Recorded delivery eligibility; sender pacing and connection state can also delay execution.")
        if reason and not row["error"]:
            row["error"] = reason
    source = batch.message(ai.get("source_inbound_message_id"), instagram=instagram,
                           lead_id=lead.pk if lead else None, account_id=message.account_id)
    if source and source.direction == "inbound":
        _related(row, "instagram" if instagram else "whatsapp", source, "Source inbound message")
    _history(row, "Message recorded", message.created_at, message.message_type)
    if message.sent_at:
        _history(row, "Provider send accepted", message.sent_at, "Provider acceptance is separate from delivered/read status.")
    _history(row, "Latest saved message state", message.updated_at, row["status_label"] + "; this observation time is not necessarily the provider receipt time.")


def _project(source, item, event, batch):
    kind = _SOURCES[source]
    public_id = f"{_QUEUE_PREFIXES[source]}:{item.pk}" if source in _QUEUE_PREFIXES else str(item.pk)
    row = _empty_row(kind, public_id, event["activity_time"])
    if source in {"whatsapp", "instagram"}:
        _message_row(row, item, batch, instagram=source == "instagram")
        return row
    lead = _object_lead(item)
    _lead(row, lead, batch)
    row["direction"] = "internal"
    row["updated_at"] = getattr(item, "updated_at", None)
    if source in {"wa_queue", "ig_queue"}:
        instagram = source == "ig_queue"
        execution = _mapping(_mapping(item.raw_payload).get("shvya_ai_execution"))
        _account(row, batch, item.account_id, instagram=instagram)
        _status(row, execution.get("status"))
        row.update(origin="Instagram AI queue" if instagram else "WhatsApp API AI queue",
                   content="AI work for: " + _text(item.body), content_is_preview=False,
                   attempt=execution.get("attempts") if type(execution.get("attempts")) is int else "",
                   error=safe_error_message(_text(execution.get("reason"))), updated_at=_stamp(execution.get("updated_at")))
        _related(row, "instagram" if instagram else "whatsapp", item, "Source inbound message")
        _history(row, "Inbound message recorded", item.created_at)
        _history(row, "Latest durable AI queue state", row["updated_at"], row["status_label"])
    elif source == "hosted_queue":
        _account(row, batch, item.account_id)
        _status(row, item.status)
        row.update(origin="Welcome queue" if item.kind == "welcome" else "Hosted AI queue",
                   scheduled_at=item.available_at, started_at=item.started_at, completed_at=item.completed_at,
                   attempt=item.attempts, error=safe_error_message(item.error),
                   content="Welcome message awaiting execution" if item.kind == "welcome" else "AI reply work")
        inbound = batch.message(item.source_message_id, lead_id=item.lead_id, account_id=item.account_id)
        outbound = batch.message(_mapping(item.result).get("message_id"), lead_id=item.lead_id, account_id=item.account_id)
        if inbound:
            row["content"] = "AI work for: " + _text(inbound.body)
            _related(row, "whatsapp", inbound, "Source inbound message")
        if outbound and outbound.direction == "outbound":
            _related(row, "whatsapp", outbound, "Saved outbound message")
        _history(row, "Queue job created", item.created_at)
        _history(row, "Eligible to run", item.available_at, "Eligibility can also depend on current connection, sender pacing and account health.")
        if item.started_at:
            _history(row, "Worker started", item.started_at)
        if item.completed_at:
            _history(row, "Queue execution ended", item.completed_at, row["status_label"] + "; check the message record for delivery.")
    elif source == "ai":
        _status(row, item.status)
        row.update(origin="AI execution", content=_text(item.response_preview or item.incoming_message_preview),
                   content_is_preview=True, started_at=item.started_at, completed_at=item.completed_at,
                   model=_text(item.model_name), error=safe_error_message(item.reason_code))
        instagram = item.connection_type == "instagram"
        inbound = batch.message(item.source_inbound_message_id, instagram=instagram, lead_id=item.lead_id,
                                account_id=item.whatsapp_account_id if not instagram else None)
        outbound = batch.message(item.outbound_message_id, instagram=instagram, lead_id=item.lead_id,
                                 account_id=item.whatsapp_account_id if not instagram else (inbound.account_id if inbound else None))
        account_id = inbound.account_id if inbound else item.whatsapp_account_id
        _account(row, batch, account_id, instagram=instagram)
        if inbound and inbound.direction == "inbound":
            _related(row, "instagram" if instagram else "whatsapp", inbound, "Source inbound message")
        if outbound and outbound.direction == "outbound":
            _related(row, "instagram" if instagram else "whatsapp", outbound, "Saved outbound message")
        historical_pipeline = batch.pipelines.get(str(item.pipeline_id))
        historical_stage = batch.stages.get(str(item.stage_id))
        if historical_pipeline:
            detail = historical_pipeline.name
            if historical_stage and historical_stage.pipeline_id == historical_pipeline.pk:
                detail += " / " + historical_stage.name
            _history(row, "CRM context when AI started", item.started_at, detail + ". The table's pipeline and owner are the current CRM assignment.")
        _history(row, "AI execution started", item.started_at)
        if item.completed_at:
            _history(row, "AI execution ended", item.completed_at, row["status_label"] + "; this is not a message delivery receipt.")
        usage = _mapping(_mapping(item.details).get("usage"))
        if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
            row["tokens"] = max(0, usage["input_tokens"]) + max(0, usage["output_tokens"])
    elif source in {"cadence", "sequence"}:
        sequence = item.sequence
        row.update(sequence_name=_text(sequence.name), origin="Cadence", scheduled_at=item.scheduled_for if source == "cadence" else item.upcoming_send_at)
        _status(row, item.status)
        if source == "sequence":
            _account(row, batch, sequence.instagram_account_id or sequence.whatsapp_account_id, instagram=bool(sequence.instagram_account_id))
            row["content"] = _text(item.next_step.title) if item.next_step else "No next step recorded"
            row["origin"] = "Current Cadence progress"
            _history(row, "Sequence assigned", item.assigned_at)
            if item.activated_at:
                _history(row, "Sequence activated", item.activated_at)
            if item.last_sent_at:
                _history(row, "Last recorded sequence send", item.last_sent_at)
            _history(row, "Current progress saved", item.updated_at, "This row is the present state; individual attempts are separate Cadence records.")
            if not item.lead_auto_followup_enabled or not lead.auto_followup_enabled:
                row["error"] = "Lead follow-ups are currently disabled."
        else:
            row.update(started_at=item.started_at, completed_at=item.finished_at, attempt=item.attempt_no,
                       error=safe_error_message(item.error), content=_text(item.step.title), direction="outbound", message_type=item.step.step_type)
            if item.step.step_type == "email":
                row.update(channel="Email", to_number=_text(lead.email), content=_text(_mapping(item.payload).get("subject") or item.step.title), external_id=_text(item.email_message_id))
            elif item.step.step_type == "reminder":
                row.update(channel="CRM reminder", direction="internal")
            else:
                instagram = item.step.step_type == "instagram"
                message = batch.message(_mapping(item.payload).get("instagram_message_id") if instagram else item.whatsapp_message_id,
                                        instagram=instagram, lead_id=item.lead_id)
                if message is not None and message.direction != "outbound":
                    message = None
                _account(row, batch, message.account_id if message else (sequence.instagram_account_id if instagram else sequence.whatsapp_account_id), instagram=instagram)
                if message and message.direction == "outbound":
                    row.update(content=_text(message.body), sent_at=message.sent_at, external_id=_text(message.external_id),
                               from_number=_text(message.sender_id if instagram else message.from_number),
                               to_number=_text(message.recipient_id if instagram else message.to_number),
                               message_type=_text(message.message_type))
                    if not instagram and _mapping(message.media_payload).get("transport") == "template":
                        row["message_type"] = "template"
                    _related(row, "instagram" if instagram else "whatsapp", message, "Saved outbound message")
                else:
                    _history(row, "Outbound message unavailable", detail="No validated outbound message is available for this attempt. The displayed connection belongs to its sequence configuration.")
            _history(row, "Attempt recorded", item.created_at)
            _history(row, "Scheduled for", item.scheduled_for)
            if item.started_at:
                _history(row, "Attempt started", item.started_at)
            if item.finished_at:
                _history(row, "Attempt finished", item.finished_at, row["status_label"] + "; the associated message owns delivery status.")
            if item.next_retry_at:
                _history(row, "Retry scheduled", item.next_retry_at)
    elif source == "workflow":
        _status(row, item.status)
        row.update(origin="Workflow", content=_text(item.rule.name) + " · " + _text(item.action_type),
                   error=safe_error_message(item.detail), scheduled_at=item.due_at, completed_at=item.finished_at)
        if item.action_type == "message":
            row["direction"] = "outbound"
            message = batch.message(item.message_id, lead_id=item.lead_id)
            _account(row, batch, message.account_id if message else _uuid(_mapping(item.action).get("account")))
            if message:
                row.update(content=_text(message.body), sent_at=message.sent_at, external_id=_text(message.external_id),
                           from_number=_text(message.from_number), to_number=_text(message.to_number), message_type=_text(message.message_type))
                _related(row, "whatsapp", message, "Workflow outbound message")
        elif item.action_type == "email":
            row.update(channel="Email", direction="outbound", to_number=_text(lead.email))
        _history(row, "Workflow run recorded", item.created_at, item.rule.name)
        _history(row, "Scheduled for", item.due_at)
        if item.finished_at:
            _history(row, "Workflow execution ended", item.finished_at, row["status_label"])
    return row


def _hydrate(organization, events):
    grouped = defaultdict(list)
    for event in events:
        value = event["activity_id"].partition(":")[2] if event["activity_source"] in _QUEUE_PREFIXES else event["activity_id"]
        grouped[event["activity_source"]].append(_uuid(value))
    objects = {
        source: {str(item.pk): item for item in _select_related(_source_base(organization, source).filter(pk__in=identifiers), source)}
        for source, identifiers in grouped.items()
    }
    batch = _Batch(organization, objects)
    rows = []
    for event in events:
        source = event["activity_source"]
        raw_id = event["activity_id"].partition(":")[2] if source in _QUEUE_PREFIXES else event["activity_id"]
        item = objects[source].get(_uuid(raw_id))
        if item is not None:  # A concurrent legitimate deletion can remove a selected record.
            rows.append(_project(source, item, event, batch))
    return rows


def activity_count(organization, filters):
    return _index(organization, filters).count()


def activity_page(organization, filters, page=1, per_page=50):
    try:
        page, per_page = int(page), int(per_page)
    except (TypeError, ValueError):
        raise ValueError("Choose a valid activity page.") from None
    if page < 1 or not 1 <= per_page <= 200:
        raise ValueError("Choose a positive page and a page size between 1 and 200.")
    index = _index(organization, filters)
    total = index.count()
    num_pages = max(1, math.ceil(total / per_page))
    page = min(page, num_pages)
    offset = (page - 1) * per_page
    rows = _hydrate(organization, list(index[offset:offset + per_page]))
    return {
        "rows": rows, "total": total, "page": page, "num_pages": num_pages,
        "has_previous": page > 1, "has_next": page < num_pages,
        "summary": [
            {"label": "Matching activity records", "value": total},
            {"label": "On this page", "value": len(rows)},
            {"label": "Needs attention on this page", "value": sum(row["outcome"] == "attention" for row in rows)},
            {"label": "Waiting on this page", "value": sum(row["outcome"] == "waiting" for row in rows)},
        ],
    }


def iter_activity(organization, filters):
    iterator = _index(organization, filters).iterator(chunk_size=_CHUNK)
    try:
        while events := list(islice(iterator, _CHUNK)):
            yield from _hydrate(organization, events)
    finally:
        iterator.close()


def _add_detail_relations(organization, row, source, identifier):
    """Add exact, bounded cross-record links without time/phone coincidences."""
    if source not in {"whatsapp", "instagram", "wa_queue", "ig_queue"}:
        return
    instagram = source in {"instagram", "ig_queue"}
    model_kind = "instagram" if instagram else "whatsapp"
    message = _source_base(organization, model_kind).filter(pk=identifier).first()
    if message is None:
        return
    lead_id = message.conversation.lead_id if instagram else message.lead_id
    if lead_id:
        reference = "source_inbound_message_id" if message.direction == "inbound" else "outbound_message_id"
        traces = _source_base(organization, "ai").filter(lead_id=lead_id, **{reference: message.pk})
        traces = traces.filter(connection_type="instagram") if instagram else traces.exclude(connection_type="instagram").filter(whatsapp_account_id=message.account_id)
        for trace in traces.order_by("-started_at", "-pk")[:_DETAIL_LIMIT]:
            _related(row, "ai", trace, "AI execution · " + trace.status)
        executions = _execution_base(organization).filter(lead_id=lead_id) if message.direction == "outbound" else _execution_base(organization).none()
        executions = (executions.filter(payload__instagram_message_id=str(message.pk), step__step_type="instagram")
                      if instagram else executions.filter(whatsapp_message_id=message.pk))
        for execution in executions.order_by("-created_at", "-pk")[:_DETAIL_LIMIT]:
            _related(row, "cadence", execution, "Cadence attempt · " + execution.status)
        if not instagram:
            for run in _workflow_base(organization).filter(message=message).order_by("-created_at", "-pk")[:_DETAIL_LIMIT]:
                _related(row, "workflow", run, "Workflow execution · " + run.status)
            jobs = _source_base(organization, "hosted_queue").filter(lead_id=lead_id, account_id=message.account_id).filter(
                Q(source_message_id=message.pk) | Q(result__message_id=str(message.pk)),
            )
            for job in jobs.order_by("-created_at", "-pk")[:_DETAIL_LIMIT]:
                _related(row, "queue", job, "Hosted AI queue · " + job.status, prefix="hosted:")
        queue_source = "ig_queue" if instagram else "wa_queue"
        if (message.direction == "inbound"
                and isinstance(_mapping(message.raw_payload).get("shvya_ai_execution"), dict)
                and _source_base(organization, queue_source).filter(pk=message.pk).exists()):
            _related(row, "queue", message, "Durable AI queue state", prefix="ig:" if instagram else "wa:")
    from apps.core.api_activity.models import APIActivity
    observations = list(APIActivity.objects.filter(
        organization=organization, entity_type="message", entity_id=str(message.pk),
        connection_id=str(message.account_id),
        integration__in=["instagram"] if instagram else ["whatsapp_api", "whatsapp_hosted", "whatsapp_coexistence"],
    ).order_by("-created_at", "-pk").values("created_at", "summary", "status")[:_DETAIL_LIMIT + 1])
    if len(observations) > _DETAIL_LIMIT:
        _history(row, "Observation history is bounded", detail="Showing the latest 100 retained API observations; older canonical activity remains available through date filters.")
    for observation in reversed(observations[:_DETAIL_LIMIT]):
        _history(row, "Recorded API observation", observation["created_at"], safe_error_message(observation["summary"]))
    _history(row, "History coverage", detail="Saved timestamps and retained observations only. API observations can expire; absent receipts do not establish delivery or failure. Related lists show at most 100 records per source; the activity feed retains all matching records.")


def get_activity(organization, kind, identifier):
    source = kind
    if kind == "queue":
        prefix, separator, identifier = str(identifier).partition(":")
        source = {value: key for key, value in _QUEUE_PREFIXES.items()}.get(prefix, "") if separator else ""
    identifier = _uuid(identifier)
    if source not in _SOURCES or not identifier or _SOURCES[source] != kind:
        raise ValueError("The activity record is unavailable.")
    records = _annotate_source(_source_base(organization, source).filter(pk=identifier), source)
    events = list(_index_part(source, kind, records))
    if not events:
        raise ValueError("The activity record is unavailable in this organisation.")
    rows = _hydrate(organization, events)
    if not rows:
        raise ValueError("The activity record is unavailable in this organisation.")
    row = rows[0]
    _add_detail_relations(organization, row, source, identifier)
    _history(row, "CRM assignment", detail="Pipeline, stage and owner filters use the lead's current CRM assignment. The connection is the recorded sender, including historical inactive connections.")
    return row
