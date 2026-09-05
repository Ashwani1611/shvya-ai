"""Business logic for the SHVYA Co-Pilot lead monitor.

The implementation deliberately reads SHVYA's existing CRM, call and
WhatsApp models.  A few fields described by the original Co-Pilot product
specification (intent score and sequence state) do not exist as first-class
SHVYA columns yet.  For those signals we use a conservative adapter over
``Lead.attributes`` and only fire a flag when the corresponding source data
is actually present.  This prevents Co-Pilot from inventing automation state
for existing leads.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.copilot.models import CopilotLeadFlag, CopilotScanState
from apps.crm.models import Lead, LeadCall, Pipeline, Stage


SEVERITY_RANK = {
    CopilotLeadFlag.Severity.LOW: 1,
    CopilotLeadFlag.Severity.MEDIUM: 2,
    CopilotLeadFlag.Severity.HIGH: 3,
    CopilotLeadFlag.Severity.CRITICAL: 4,
}

FLAG_DEFINITIONS = {
    "R1": {
        "label": "Reply Pending",
        "description": "A lead is waiting for a human reply.",
        "tone": "red",
    },
    "R2": {
        "label": "New Lead, No Contact",
        "description": "A new lead has not been contacted yet.",
        "tone": "amber",
    },
    "R3": {
        "label": "Delivery Failure / Delay",
        "description": "Recent outreach failed or a scheduled send is overdue.",
        "tone": "orange",
    },
    "C1": {
        "label": "No Calls Ever Made",
        "description": "Messaging started, but no call is logged.",
        "tone": "amber",
    },
    "C2": {
        "label": "Call Gap",
        "description": "It has been a while since the last call.",
        "tone": "orange",
    },
    "C3": {
        "label": "All Calls No Response",
        "description": "Repeated calls have not been answered.",
        "tone": "orange",
    },
    "H1": {
        "label": "High Intent, No Action",
        "description": "High buying intent without recent outbound action.",
        "tone": "red",
    },
    "H2": {
        "label": "Was Engaging, Now Silent",
        "description": "The lead has gone quiet.",
        "tone": "red",
    },
    "H3": {
        "label": "No Automation Running",
        "description": "No active sequence or scheduled follow-up is available.",
        "tone": "orange",
    },
    "S1": {
        "label": "Follow-ups Exhausted, Lead Silent",
        "description": "The configured follow-up run ended without a reply.",
        "tone": "orange",
    },
    "S2": {
        "label": "Sequence Complete, No Stage Move",
        "description": "Automation finished but the lead stayed in the same stage.",
        "tone": "amber",
    },
    "X2": {
        "label": "No Phone Number",
        "description": "WhatsApp outreach cannot continue without a phone number.",
        "tone": "orange",
    },
    "X3": {
        "label": "Stage Stale",
        "description": "The lead has remained in the same stage for too long.",
        "tone": "amber",
    },
    "X4": {
        "label": "Long-Term Dormant",
        "description": "No meaningful activity has happened for a long period.",
        "tone": "red",
    },
}

DEFAULT_CONFIG = {
    "copilot_enabled": False,
    "copilot_call_flags_enabled": True,
    "copilot_intent_threshold": 70,
    "copilot_followup_threshold": 3,
    "copilot_thresholds": {},
    "copilot_exempt_stages": [],
    "copilot_hot_stages": [],
}

SNOOZE_MAX = {
    "R1": timedelta(hours=6),
    "R2": timedelta(hours=4),
    "R3": None,
    "C1": timedelta(days=2),
    "C2": timedelta(days=2),
    "C3": timedelta(days=3),
    "H1": timedelta(hours=2),
    "H2": timedelta(days=2),
    "H3": timedelta(days=1),
    "S1": timedelta(days=2),
    "S2": timedelta(days=3),
    "X2": timedelta(days=3),
    "X3": timedelta(days=7),
    "X4": timedelta(days=7),
}

DURATION_MAP = {
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "1d": timedelta(days=1),
    "2d": timedelta(days=2),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}


def get_copilot_config(organization) -> dict[str, Any]:
    settings = organization.settings or {}
    result = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in settings:
            result[key] = settings[key]

    # Defensive normalization for older or hand-edited organization JSON.
    try:
        result["copilot_intent_threshold"] = int(
            result["copilot_intent_threshold"]
        )
    except (TypeError, ValueError):
        result["copilot_intent_threshold"] = 70

    try:
        result["copilot_followup_threshold"] = int(
            result["copilot_followup_threshold"]
        )
    except (TypeError, ValueError):
        result["copilot_followup_threshold"] = 3

    result["copilot_call_flags_enabled"] = bool(
        result["copilot_call_flags_enabled"]
    )
    result["copilot_enabled"] = bool(result["copilot_enabled"])
    result["copilot_exempt_stages"] = (
        result["copilot_exempt_stages"]
        if isinstance(result["copilot_exempt_stages"], list)
        else []
    )
    result["copilot_hot_stages"] = (
        result["copilot_hot_stages"]
        if isinstance(result["copilot_hot_stages"], list)
        else []
    )
    return result


def update_copilot_config(organization, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist only supported Co-Pilot organization settings."""

    current = get_copilot_config(organization)
    allowed = set(DEFAULT_CONFIG)
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unsupported Co-Pilot setting: {sorted(unknown)[0]}")

    if "copilot_enabled" in payload:
        current["copilot_enabled"] = _as_bool(payload["copilot_enabled"])

    if "copilot_call_flags_enabled" in payload:
        current["copilot_call_flags_enabled"] = _as_bool(
            payload["copilot_call_flags_enabled"]
        )

    if "copilot_intent_threshold" in payload:
        value = _bounded_int(payload["copilot_intent_threshold"], 0, 100)
        current["copilot_intent_threshold"] = value

    if "copilot_followup_threshold" in payload:
        value = _bounded_int(payload["copilot_followup_threshold"], 1, 20)
        current["copilot_followup_threshold"] = value

    if "copilot_thresholds" in payload:
        if not isinstance(payload["copilot_thresholds"], dict):
            raise ValueError("copilot_thresholds must be an object.")
        current["copilot_thresholds"] = payload["copilot_thresholds"]

    for key in ("copilot_exempt_stages", "copilot_hot_stages"):
        if key in payload:
            current[key] = _validate_stage_pairs(
                organization,
                payload[key],
            )

    settings = dict(organization.settings or {})
    settings.update(current)
    organization.settings = settings
    organization.save(update_fields=["settings", "updated_at"])
    return current


def visible_pipelines_for_user(user):
    if not user or not user.organization_id:
        return Pipeline.objects.none()
    if user.role == User.Role.ADMIN:
        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            is_active=True,
        )
    if user.role == User.Role.AGENT:
        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            owner=user,
            is_active=True,
        )
    return Pipeline.objects.none()


def active_flags_for_user(
    user,
    *,
    pipeline_id: str | None = None,
    severity: str | None = None,
    flag_code: str | None = None,
):
    """Return active, unsnoozed flags within the CRM user's pipeline scope."""

    if not user or not user.organization_id:
        return CopilotLeadFlag.objects.none()

    pipelines = visible_pipelines_for_user(user)
    now = timezone.now()
    qs = (
        CopilotLeadFlag.objects.filter(
            organization_id=user.organization_id,
            lead__pipeline__in=pipelines,
            resolved_at__isnull=True,
        )
        .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))
        .select_related("lead", "lead__pipeline", "lead__stage")
    )

    if pipeline_id:
        qs = qs.filter(lead__pipeline_id=pipeline_id)
    if severity in SEVERITY_RANK:
        qs = qs.filter(severity=severity)
    if flag_code in FLAG_DEFINITIONS:
        qs = qs.filter(flag_code=flag_code)

    return qs


def group_flags_for_dashboard(flags) -> list[dict[str, Any]]:
    """Collapse one-row-per-flag storage into one user-friendly card per lead."""

    by_lead: dict[Any, list[CopilotLeadFlag]] = defaultdict(list)
    for flag in flags:
        by_lead[flag.lead_id].append(flag)

    cards = []
    for lead_flags in by_lead.values():
        lead_flags.sort(
            key=lambda item: (
                item.flag_code == "H1",
                SEVERITY_RANK[item.severity],
                item.last_detected_at,
            ),
            reverse=True,
        )
        primary = lead_flags[0]
        lead = primary.lead
        cards.append(
            {
                "lead": lead,
                "severity": primary.severity,
                "primary_flag": primary,
                "flags": [flag_payload(flag) for flag in lead_flags],
                "quick_view_url": reverse(
                    "crm-lead-detail",
                    kwargs={"lead_id": lead.id},
                ),
                "send_message_url": reverse(
                    "whatsapp-chat-detail",
                    kwargs={"lead_id": lead.id},
                ),
            }
        )

    cards.sort(
        key=lambda card: (
            card["primary_flag"].flag_code == "H1",
            SEVERITY_RANK[card["severity"]],
            card["primary_flag"].last_detected_at,
        ),
        reverse=True,
    )
    return cards


def flag_payload(flag: CopilotLeadFlag) -> dict[str, Any]:
    definition = FLAG_DEFINITIONS[flag.flag_code]
    return {
        "id": str(flag.id),
        "code": flag.flag_code,
        "label": definition["label"],
        "description": definition["description"],
        "tone": definition["tone"],
        "severity": flag.severity,
        "metadata": flag.metadata,
        "can_snooze": can_snooze(flag.flag_code, flag.severity),
        "snooze_options": snooze_options(flag.flag_code, flag.severity),
    }


def can_snooze(flag_code: str, severity: str) -> bool:
    if flag_code == "R3":
        return False
    if flag_code == "H2" and severity == CopilotLeadFlag.Severity.CRITICAL:
        return False
    if flag_code == "X4" and severity == CopilotLeadFlag.Severity.CRITICAL:
        return False
    return SNOOZE_MAX.get(flag_code) is not None


def snooze_options(flag_code: str, severity: str) -> list[str]:
    if not can_snooze(flag_code, severity):
        return []
    maximum = SNOOZE_MAX[flag_code]
    return [
        key for key, duration in DURATION_MAP.items() if duration <= maximum
    ]


def snooze_flag(flag: CopilotLeadFlag, duration_key: str) -> CopilotLeadFlag:
    if not can_snooze(flag.flag_code, flag.severity):
        raise ValueError("This flag cannot be snoozed at its current severity.")
    duration = DURATION_MAP.get(duration_key)
    if duration is None:
        raise ValueError("Unsupported snooze duration.")
    maximum = SNOOZE_MAX[flag.flag_code]
    if duration > maximum:
        raise ValueError("Snooze duration exceeds the maximum for this flag.")
    flag.snoozed_until = timezone.now() + duration
    flag.save(update_fields=["snoozed_until", "updated_at"])
    return flag


def resolve_flag(flag: CopilotLeadFlag) -> CopilotLeadFlag:
    flag.resolved_at = timezone.now()
    flag.save(update_fields=["resolved_at", "updated_at"])
    return flag


def ensure_fresh_cache(organization, max_age_minutes: int = 30) -> None:
    """Guarantee a usable cache when the dashboard is opened.

    Celery Beat performs the normal refresh.  This synchronous fallback is
    intentional so a newly enabled organization does not see an empty page
    just because its first Beat interval has not happened yet.
    """

    config = get_copilot_config(organization)
    if not config["copilot_enabled"]:
        return

    state = CopilotScanState.objects.filter(organization=organization).first()
    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    if state is None or state.last_refreshed_at is None or state.last_refreshed_at < cutoff:
        refresh_organization_flags(organization)


@transaction.atomic
def refresh_organization_flags(organization) -> int:
    """Evaluate all supported signals and update the persisted flag cache."""

    config = get_copilot_config(organization)
    state, _ = CopilotScanState.objects.select_for_update().get_or_create(
        organization=organization
    )

    if not config["copilot_enabled"]:
        CopilotLeadFlag.objects.filter(organization=organization).delete()
        state.last_refreshed_at = timezone.now()
        state.last_error = ""
        state.save(update_fields=["last_refreshed_at", "last_error", "updated_at"])
        return 0

    exempt_stage_ids = {
        str(item.get("stage_id"))
        for item in config["copilot_exempt_stages"]
        if isinstance(item, dict) and item.get("stage_id")
    }

    leads = (
        Lead.objects.filter(organization=organization)
        .exclude(stage_id__in=exempt_stage_ids)
        .select_related("pipeline", "stage")
        .prefetch_related(
            Prefetch(
                "whatsapp_messages",
                queryset=WhatsAppMessage.objects.filter(
                    organization=organization
                ).order_by("created_at"),
                to_attr="_copilot_messages",
            ),
            Prefetch(
                "calls",
                queryset=LeadCall.objects.order_by("called_at", "created_at"),
                to_attr="_copilot_calls",
            ),
        )
    )

    account_modes = _pipeline_api_modes(organization)
    total = 0

    for lead in leads.iterator(chunk_size=250):
        signals = evaluate_lead(
            lead,
            config=config,
            pipeline_uses_api=account_modes.get(str(lead.pipeline_id), False),
        )
        _persist_lead_signals(organization, lead, signals)
        total += len(signals)

    # Remove cache rows for leads that no longer exist in the evaluated set is
    # handled by FK cascade. Exempt-stage rows need explicit cleanup.
    if exempt_stage_ids:
        CopilotLeadFlag.objects.filter(
            organization=organization,
            lead__stage_id__in=exempt_stage_ids,
        ).delete()

    state.last_refreshed_at = timezone.now()
    state.last_error = ""
    state.save(update_fields=["last_refreshed_at", "last_error", "updated_at"])
    return total


def evaluate_lead(
    lead: Lead,
    *,
    config: dict[str, Any],
    pipeline_uses_api: bool,
    now=None,
) -> list[dict[str, Any]]:
    now = now or timezone.now()
    messages = list(getattr(lead, "_copilot_messages", []))
    calls = list(getattr(lead, "_copilot_calls", []))

    inbound = [m for m in messages if m.direction == "inbound"]
    outbound = [m for m in messages if m.direction == "outbound"]
    human_outbound = [m for m in outbound if not _is_automated_outbound(m)]

    last_inbound = inbound[-1] if inbound else None
    last_outbound = outbound[-1] if outbound else None
    last_human_outbound = human_outbound[-1] if human_outbound else None
    attrs = lead.attributes if isinstance(lead.attributes, dict) else {}

    signals: list[dict[str, Any]] = []

    # R1 / R2 are mutually exclusive by construction.
    if last_inbound and (
        last_human_outbound is None
        or last_inbound.created_at > last_human_outbound.created_at
    ):
        gap_hours = _hours(now - last_inbound.created_at)
        severity = _severity_by_hours(
            gap_hours,
            [(24, "critical"), (6, "high"), (2, "medium")],
        )
        if severity:
            signals.append(
                _signal(
                    "R1",
                    severity,
                    gap_hours=int(gap_hours),
                    last_lead_message_preview=(last_inbound.body or "")[:140],
                )
            )
    elif not outbound and not calls:
        age_hours = _hours(now - lead.created_at)
        severity = _severity_by_hours(
            age_hours,
            [(24, "critical"), (12, "high"), (4, "medium"), (1, "low")],
        )
        if severity:
            signals.append(
                _signal("R2", severity, hours_since_created=int(age_hours))
            )

    # R3: Cloud API uses consecutive failed outbound sends. The extension
    # path is supported when a real upcoming_send_at value is present in
    # Lead.attributes; SHVYA currently has no dedicated column for it.
    if pipeline_uses_api:
        consecutive_failed = 0
        last_status = ""
        for message in reversed(outbound):
            if not last_status:
                last_status = message.status
            if message.status in {"failed", "undelivered"}:
                consecutive_failed += 1
            else:
                break
        if consecutive_failed >= 2:
            signals.append(
                _signal(
                    "R3",
                    "high" if consecutive_failed >= 3 else "medium",
                    path="api",
                    failed_count=consecutive_failed,
                    last_status=last_status,
                )
            )
    else:
        upcoming_send_at = _attribute_datetime(attrs.get("upcoming_send_at"))
        if upcoming_send_at and upcoming_send_at < now:
            successful_after = any(
                message.created_at >= upcoming_send_at
                and message.status in {"sent", "delivered", "read"}
                for message in outbound
            )
            if not successful_after:
                overdue_hours = _hours(now - upcoming_send_at)
                if overdue_hours >= 1:
                    signals.append(
                        _signal(
                            "R3",
                            "high" if overdue_hours >= 6 else "medium",
                            path="extension",
                            overdue_hours=int(overdue_hours),
                            last_status=(outbound[-1].status if outbound else "overdue"),
                        )
                    )

    if config["copilot_call_flags_enabled"]:
        if not calls and outbound:
            age_days = _days(now - lead.created_at)
            severity = _severity_by_days(
                age_days,
                [(10, "critical"), (5, "high"), (3, "medium")],
            )
            if severity:
                signals.append(
                    _signal("C1", severity, days_since_created=int(age_days))
                )

        if calls:
            last_call_at = max(
                (call.called_at or call.created_at) for call in calls
            )
            call_gap_days = _days(now - last_call_at)
            severity = _severity_by_days(
                call_gap_days,
                [(5, "high"), (3, "medium"), (2, "low")],
            )
            if severity:
                signals.append(
                    _signal(
                        "C2",
                        severity,
                        days_since_last_call=int(call_gap_days),
                        last_call_at=last_call_at.isoformat(),
                    )
                )

            if all(call.status == "no_response" for call in calls) and len(calls) >= 3:
                signals.append(
                    _signal(
                        "C3",
                        "high" if len(calls) >= 5 else "medium",
                        no_response_count=len(calls),
                    )
                )

    intent_score = _attribute_number(attrs.get("intent_score"))
    if intent_score is not None and intent_score > config["copilot_intent_threshold"]:
        outbound_at = last_outbound.created_at if last_outbound else lead.created_at
        gap_hours = _hours(now - outbound_at)
        if gap_hours >= 24:
            signals.append(
                _signal(
                    "H1",
                    "critical",
                    intent_score=int(intent_score),
                    hours_since_last_outbound=int(gap_hours),
                )
            )

    silent_since = last_inbound.created_at if last_inbound else lead.created_at
    silent_days = _days(now - silent_since)
    h2_severity = _severity_by_days(
        silent_days,
        [(7, "critical"), (5, "high"), (3, "medium")],
    )
    if h2_severity:
        signals.append(
            _signal(
                "H2",
                h2_severity,
                days_silent=int(silent_days),
                last_lead_message_preview=(last_inbound.body or "")[:140]
                if last_inbound
                else "",
                had_prior_inbound=bool(last_inbound),
            )
        )

    sequence_state = _sequence_state(attrs)
    if sequence_state["supported"]:
        if not sequence_state["active_sequence_id"] and not sequence_state["upcoming_send_at"]:
            age_hours = _hours(now - lead.created_at)
            if age_hours >= 24:
                signals.append(
                    _signal(
                        "H3",
                        "high" if age_hours >= 48 else "medium",
                        hours_since_created=int(age_hours),
                        has_upcoming_send=False,
                    )
                )

        followup_count = sequence_state["followup_count"]
        last_followup_at = sequence_state["last_followup_at"]
        if (
            followup_count >= config["copilot_followup_threshold"]
            and last_followup_at
            and (last_inbound is None or last_inbound.created_at < last_followup_at)
        ):
            signals.append(
                _signal(
                    "S1",
                    "high",
                    followup_count=followup_count,
                    last_followup_at=last_followup_at.isoformat(),
                    last_inbound_at=(
                        last_inbound.created_at.isoformat() if last_inbound else None
                    ),
                )
            )

        sequence_completed_at = sequence_state["sequence_completed_at"]
        if (
            sequence_completed_at
            and not sequence_state["active_sequence_id"]
            and lead.stage_entered_at < sequence_completed_at
        ):
            signals.append(
                _signal(
                    "S2",
                    "medium",
                    last_followup_at=(
                        last_followup_at.isoformat() if last_followup_at else None
                    ),
                    days_since_stage_change=int(_days(now - lead.stage_entered_at)),
                    stage_name=lead.stage.name,
                )
            )

    if not (lead.phone or "").strip():
        signals.append(_signal("X2", "high"))

    stage_days = _days(now - lead.stage_entered_at)
    stage_severity = _severity_by_days(
        stage_days,
        [(30, "critical"), (21, "high"), (14, "medium")],
    )
    if stage_severity:
        signals.append(
            _signal(
                "X3",
                stage_severity,
                days_in_stage=int(stage_days),
                stage_name=lead.stage.name,
            )
        )

    activity_times = [lead.stage_entered_at]
    activity_times.extend(m.created_at for m in messages)
    activity_times.extend((c.called_at or c.created_at) for c in calls)
    last_activity_at = max(activity_times) if activity_times else lead.created_at
    dormant_days = _days(now - last_activity_at)
    dormant_severity = _severity_by_days(
        dormant_days,
        [(60, "critical"), (30, "high"), (20, "medium")],
    )
    if dormant_severity:
        signals.append(
            _signal(
                "X4",
                dormant_severity,
                dormant_days=int(dormant_days),
                last_activity_at=last_activity_at.isoformat(),
            )
        )

    # H2 suppresses C2. H3 is a root-cause signal and suppresses every other
    # signal for that lead exactly as defined in the Co-Pilot rules.
    if any(item["flag_code"] == "H2" for item in signals):
        signals = [item for item in signals if item["flag_code"] != "C2"]
    if any(item["flag_code"] == "H3" for item in signals):
        signals = [item for item in signals if item["flag_code"] == "H3"]

    return signals


def _persist_lead_signals(organization, lead, signals) -> None:
    current = {
        flag.flag_code: flag
        for flag in CopilotLeadFlag.objects.filter(
            organization=organization,
            lead=lead,
        )
    }
    active_codes = {item["flag_code"] for item in signals}

    for item in signals:
        existing = current.get(item["flag_code"])
        if existing is None:
            CopilotLeadFlag.objects.create(
                organization=organization,
                lead=lead,
                flag_code=item["flag_code"],
                severity=item["severity"],
                metadata=item["metadata"],
            )
            continue

        # A manually resolved flag remains hidden while the same underlying
        # condition remains true. Once the condition clears the row is deleted,
        # allowing a genuinely new occurrence to be raised later.
        if existing.resolved_at is not None:
            continue

        existing.severity = item["severity"]
        existing.metadata = item["metadata"]
        if not can_snooze(existing.flag_code, existing.severity):
            existing.snoozed_until = None
        existing.save(
            update_fields=[
                "severity",
                "metadata",
                "snoozed_until",
                "last_detected_at",
                "updated_at",
            ]
        )

    stale_ids = [
        flag.id for code, flag in current.items() if code not in active_codes
    ]
    if stale_ids:
        CopilotLeadFlag.objects.filter(id__in=stale_ids).delete()


def _pipeline_api_modes(organization) -> dict[str, bool]:
    accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
            connection_type="api",
        )
    )
    account_numbers = {
        _digits(account.display_phone_number)
        for account in accounts
        if account.display_phone_number
    }
    modes = {}
    for pipeline in Pipeline.objects.filter(organization=organization):
        modes[str(pipeline.id)] = bool(
            _digits(pipeline.phone_number) in account_numbers
            if pipeline.phone_number
            else False
        )
    return modes


def _is_automated_outbound(message: WhatsAppMessage) -> bool:
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if payload.get("shvya_ai"):
        return True

    mode = str(payload.get("mode", "")).strip().lower()
    if mode and mode != "user":
        return True

    for key in (
        "automation",
        "sequence",
        "auto_responder",
        "smart_trigger",
        "broadcast",
        "campaign",
        "ai_reply",
        "bump_up",
        "welcome_sequence",
    ):
        if payload.get(key):
            return True
    return False


def _sequence_state(attrs: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "active_sequence_id",
        "active_sequence_status",
        "upcoming_send_at",
        "followup_count",
        "last_followup_at",
        "sequence_completed_at",
    }
    supported = any(key in attrs for key in keys)
    return {
        "supported": supported,
        "active_sequence_id": attrs.get("active_sequence_id"),
        "active_sequence_status": attrs.get("active_sequence_status"),
        "upcoming_send_at": _attribute_datetime(attrs.get("upcoming_send_at")),
        "followup_count": int(_attribute_number(attrs.get("followup_count")) or 0),
        "last_followup_at": _attribute_datetime(attrs.get("last_followup_at")),
        "sequence_completed_at": _attribute_datetime(
            attrs.get("sequence_completed_at")
        ),
    }


def _attribute_datetime(value):
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        result = value
    elif isinstance(value, str):
        result = parse_datetime(value)
    else:
        return None
    if result is None:
        return None
    if timezone.is_naive(result):
        result = timezone.make_aware(result, timezone.get_current_timezone())
    return result


def _attribute_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_stage_pairs(organization, value):
    if not isinstance(value, list):
        raise ValueError("Stage configuration must be a list.")

    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each stage selection must contain pipeline_id and stage_id.")
        pipeline_id = str(item.get("pipeline_id", "")).strip()
        stage_id = str(item.get("stage_id", "")).strip()
        if not pipeline_id or not stage_id:
            raise ValueError("Each stage selection must contain pipeline_id and stage_id.")

        exists = Stage.objects.filter(
            id=stage_id,
            pipeline_id=pipeline_id,
            pipeline__organization=organization,
            is_active=True,
        ).exists()
        if not exists:
            raise ValueError("Selected pipeline/stage does not belong to this organization.")

        pair = (pipeline_id, stage_id)
        if pair not in seen:
            normalized.append({"pipeline_id": pipeline_id, "stage_id": stage_id})
            seen.add(pair)
    return normalized


def _bounded_int(value, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected a whole number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Value must be between {minimum} and {maximum}.")
    return parsed


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError("Expected a boolean value.")


def _signal(flag_code, severity, **metadata):
    return {
        "flag_code": flag_code,
        "severity": severity,
        "metadata": metadata,
    }


def _hours(delta):
    return max(0.0, delta.total_seconds() / 3600)


def _days(delta):
    return max(0.0, delta.total_seconds() / 86400)


def _severity_by_hours(value, bands):
    for threshold, severity in bands:
        if value >= threshold:
            return severity
    return None


def _severity_by_days(value, bands):
    for threshold, severity in bands:
        if value >= threshold:
            return severity
    return None


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())
