"""Tenant-scoped upload review and CRM audience preparation.

The existing importer owns file parsing/phone normalization. This module owns
campaign intent, permissions and previews, and never sends a WhatsApp message.
"""
from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.campaign_models import CampaignDelivery, CampaignSuppression, CampaignUpload
from apps.channels.models import BulkMessageCampaign, BulkMessageRecipient
from apps.crm.models import AttributeDefinition, Lead, PipelinePermission
from services.crm.lead_import_service import normalize_import_phone, parse_uploaded_file

from .campaign_policy import CampaignInputError, fingerprint

CORE_SOURCES = [
    {"key": "lead_name", "label": "Lead name"},
    {"key": "lead_first_name", "label": "First name"},
    {"key": "phone", "label": "Phone"},
    {"key": "email", "label": "Email"},
    {"key": "org_name", "label": "Organization name"},
]
SENSITIVE_KEY = re.compile(r"(^_|password|secret|token|credential|api_key|authorization|session|internal)", re.I)


def uuid_value(value, label="Selection"):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CampaignInputError(f"{label} is invalid. Refresh and select it again.") from exc


def user_pipelines(user):
    from apps.crm.views.api import get_user_pipelines

    if not user or not user.is_active or not user.organization_id or user.is_superuser:
        raise PermissionDenied
    return get_user_pipelines(user).filter(organization_id=user.organization_id, is_active=True)


def rights(user, pipeline):
    names = ("can_create_leads", "can_edit_leads", "can_move_leads")
    if user.role == User.Role.ADMIN:
        return dict.fromkeys(names, True)
    permission = PipelinePermission.objects.filter(user=user, pipeline=pipeline).first()
    return {name: bool(permission and getattr(permission, name)) for name in names}


def definitions(user):
    return [definition for definition in AttributeDefinition.objects.filter(organization_id=user.organization_id)
            if not SENSITIVE_KEY.search(definition.key)]


def source_catalog(user):
    return CORE_SOURCES + [{"key": f"attr:{item.key}", "label": item.name} for item in definitions(user)]


def visible_campaigns(user):
    pipelines = user_pipelines(user).values("pk")
    hidden_snapshot = CampaignDelivery.objects.filter(campaign_id=OuterRef("pk")).filter(
        ~Q(pipeline_key__in=pipelines) | (Q(lead__isnull=False) & ~Q(lead__pipeline_id__in=pipelines))
    )
    hidden_legacy = BulkMessageRecipient.objects.filter(campaign_id=OuterRef("pk")).exclude(lead__pipeline_id__in=pipelines)
    return BulkMessageCampaign.objects.filter(
        organization_id=user.organization_id, account__organization_id=user.organization_id,
        account__connection_type="api", pipeline_id__in=pipelines,
    ).annotate(_hidden_snapshot=Exists(hidden_snapshot), _hidden_legacy=Exists(hidden_legacy)).filter(
        _hidden_snapshot=False, _hidden_legacy=False,
    )


def require_manage(user, campaign):
    if not visible_campaigns(user).filter(pk=campaign.pk).exists():
        raise PermissionDenied
    keys = set(campaign.campaign_delivery_rows.order_by().values_list("pipeline_key", flat=True).distinct())
    keys.add(campaign.pipeline_id)
    keys.update(campaign.recipients.order_by().values_list("lead__pipeline_id", flat=True).distinct())
    pipelines = list(user_pipelines(user).filter(pk__in=keys))
    if len(pipelines) != len(keys) or any(not rights(user, pipeline)["can_edit_leads"] for pipeline in pipelines):
        raise PermissionDenied


def _values(*, name, phone, email, attributes, organization, defs):
    result = {"lead_name": name, "lead_first_name": name.split()[0] if name.split() else "", "phone": phone, "email": email, "org_name": organization.name}
    for definition in defs:
        result[f"attr:{definition.key}"] = attributes.get(definition.key, "")
    return result


def _attribute_value(definition, value):
    if not value:
        return ""
    if len(value) > 8192:
        raise CampaignInputError(f"{definition.name} exceeds 8,192 characters.")
    try:
        if definition.field_type == "numeric":
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError
            return str(number)
        if definition.field_type == "date":
            return date.fromisoformat(value).isoformat()
        if definition.field_type == "datetime":
            return datetime.fromisoformat(value).isoformat()
        if definition.field_type == "option":
            matches = [str(option) for option in definition.options if str(option).casefold() == value.casefold()]
            if len(matches) != 1:
                raise ValueError
            return matches[0]
    except (ValueError, InvalidOperation) as exc:
        raise CampaignInputError(f"{definition.name} does not match its configured field type or options.") from exc
    return value


def _guard_spreadsheet(uploaded_file):
    """Bound XLSX expansion before handing the file to the canonical parser."""
    if uploaded_file and uploaded_file.name.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(uploaded_file) as archive:
                items = archive.infolist()
                expanded = sum(item.file_size for item in items)
                compressed = max(1, sum(item.compress_size for item in items))
                if len(items) > 2000 or expanded > 50 * 1024 * 1024 or (expanded > 1024 * 1024 and expanded / compressed > 200):
                    raise CampaignInputError("This workbook expands beyond the safe import limit. Export it as CSV.")
        except zipfile.BadZipFile as exc:
            raise CampaignInputError("This XLSX workbook is not valid.") from exc
        finally:
            uploaded_file.seek(0)


def create_upload(*, user, uploaded_file):
    user_pipelines(user)
    if not uploaded_file or uploaded_file.size > 10 * 1024 * 1024:
        raise CampaignInputError("Choose a CSV, XLS or XLSX file no larger than 10 MB.")
    _guard_spreadsheet(uploaded_file)
    try:
        parsed = parse_uploaded_file(uploaded_file)
    except ValidationError:
        raise
    except Exception as exc:
        raise CampaignInputError("The spreadsheet could not be read. Save a fresh CSV, XLS or XLSX file and try again.") from exc
    if not parsed["rows"]:
        raise CampaignInputError("The file has no recipient rows.")
    if len(parsed["headers"]) > 128 or any(len(value) > 8192 for row in parsed["rows"] for value in row.values()):
        raise CampaignInputError("Use at most 128 columns and 8,192 characters per cell.")
    return CampaignUpload.objects.create(
        organization_id=user.organization_id, user=user, filename=parsed["filename"][:255],
        headers=parsed["headers"], rows=parsed["rows"], expires_at=timezone.now() + timedelta(hours=2),
    )


def owned_upload(*, user, token, lock=False):
    query = CampaignUpload.objects.filter(pk=uuid_value(token, "Upload"), organization_id=user.organization_id, user=user)
    if lock:
        query = query.select_for_update()
    upload = query.first()
    if upload is None:
        raise PermissionDenied
    if upload.expires_at <= timezone.now():
        raise CampaignInputError("This upload has expired. Upload the file again.")
    return upload


def _resolve(value, objects, *, fallback, label):
    if not str(value or "").strip():
        if fallback is None:
            raise CampaignInputError(f"Choose a {label} for this row.")
        return fallback
    value = str(value).strip()
    matches = [item for item in objects if str(item.pk) == value or item.name.casefold() == value.casefold()]
    if len(matches) != 1:
        raise CampaignInputError(f"{label.title()} is unavailable or ambiguous. Use its exact ID or a unique name.")
    return matches[0]


def is_suppressed(*, organization_id, phone, lead=None, check_registry=True):
    if check_registry and CampaignSuppression.objects.filter(organization_id=organization_id, phone=phone).exists():
        return True
    if lead is not None:
        from apps.ai_engagement.services.runtime_state import STATE_KEY

        state = (lead.attributes or {}).get(STATE_KEY)
        if isinstance(state, dict) and state.get("conversation_mode") == "opt_out":
            return True
        if "[WhatsApp] Lead opted out of AI engagement." in (lead.notes or ""):
            return True
    return False


@transaction.atomic
def review_upload(*, user, token, data):
    upload = owned_upload(user=user, token=token, lock=True)
    if hasattr(upload, "plan"):
        raise CampaignInputError("This upload already belongs to a campaign. Start a new campaign to change its audience.")
    defs = definitions(user)
    fields = {"name", "phone", "email", "pipeline", "stage"} | {f"attr:{item.key}" for item in defs}
    mapping = data.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) - fields or not mapping.get("name") or not mapping.get("phone"):
        raise CampaignInputError("Map both Name and Phone; only configured CRM fields may be mapped.")
    columns = [value for value in mapping.values() if value]
    if any(not isinstance(value, str) or value not in upload.headers for value in columns) or len(set(columns)) != len(columns):
        raise CampaignInputError("Each mapped field needs a different column from this upload.")
    mode = data.get("mode", "new_only")
    if mode not in {"new_only", "existing_only", "both"}:
        raise CampaignInputError("Choose new leads, existing leads, or both.")
    update_existing = data.get("update_existing") is True
    move_existing = data.get("move_existing") is True
    if move_existing and not update_existing:
        raise CampaignInputError("Enable existing-lead updates before changing their pipeline or stage.")
    pipelines = list(user_pipelines(user).prefetch_related("stages"))
    primary = next((item for item in pipelines if str(item.pk) == str(data.get("pipeline_id"))), None)
    if primary is None or not rights(user, primary)["can_edit_leads"]:
        raise PermissionDenied
    primary_stages = [stage for stage in primary.stages.all() if stage.is_active]
    default_stage = next((stage for stage in primary_stages if str(stage.pk) == str(data.get("stage_id"))), None)
    if mode != "existing_only" and default_stage is None and not mapping.get("stage"):
        raise CampaignInputError("Choose a default stage or map the Stage column for new leads.")
    if data.get("stage_id") and default_stage is None:
        raise CampaignInputError("The default stage must belong to the selected pipeline.")
    permission_map = {str(item.pk): rights(user, item) for item in pipelines}
    pipeline_map = {str(item.pk): item for item in pipelines}
    normalized, invalid_phones = {}, {}
    for index, row in enumerate(upload.rows, start=2):
        try:
            phone = normalize_import_phone(row.get(mapping["phone"], ""))
            if not phone or not re.fullmatch(r"\+[1-9]\d{8,14}", phone):
                raise CampaignInputError("Include an international country code and a valid-length phone number.")
            normalized[index] = phone
        except (CampaignInputError, ValidationError):
            invalid_phones[index] = "Phone is missing or malformed. Include its international country code."
    existing = {lead.phone: lead for lead in Lead.objects.filter(
        organization_id=user.organization_id, phone__in=set(normalized.values()),
    ).select_related("pipeline", "stage")}
    suppressed = set(CampaignSuppression.objects.filter(
        organization_id=user.organization_id, phone__in=set(normalized.values()),
    ).values_list("phone", flat=True))
    stats = {key: 0 for key in ("rows", "eligible", "new", "existing", "invalid", "duplicate", "excluded", "suppressed")}
    stats["rows"] = len(upload.rows)
    errors, reviewed, seen = [], [], set()
    for index, row in enumerate(upload.rows, start=2):
        phone = normalized.get(index, "")
        reason, bucket = invalid_phones.get(index, ""), "invalid"
        if not reason and phone in seen:
            reason, bucket = "Duplicate normalized phone; the first row is retained.", "duplicate"
        seen.add(phone)
        lead = existing.get(phone)
        if not reason and ((mode == "new_only" and lead) or (mode == "existing_only" and not lead)):
            reason, bucket = "Excluded by the selected new/existing lead mode.", "excluded"
        if not reason and lead and str(lead.pipeline_id) not in pipeline_map:
            reason, bucket = "Recipient is not eligible or not accessible.", "excluded"
        if not reason and (phone in suppressed or (lead and is_suppressed(organization_id=user.organization_id, phone=phone, lead=lead, check_registry=False))):
            reason, bucket = "Recipient has opted out of campaign messages.", "suppressed"
        if reason:
            stats[bucket] += 1
            errors.append({"row": index, "reason": reason, "kind": bucket})
            continue
        try:
            change_route = not lead or move_existing
            target = _resolve(row.get(mapping.get("pipeline", "")), pipelines, fallback=primary, label="pipeline") if change_route else pipeline_map[str(lead.pipeline_id)]
            stages = [stage for stage in target.stages.all() if stage.is_active]
            if change_route:
                stage = _resolve(row.get(mapping.get("stage", "")), stages,
                                 fallback=default_stage if target.pk == primary.pk else None, label="stage")
            else:
                stage = lead.stage
            permitted = permission_map[str(target.pk)]
            if not lead and not permitted["can_create_leads"]:
                raise CampaignInputError("You cannot create leads in this pipeline.")
            if not permitted["can_edit_leads"]:
                raise CampaignInputError("You cannot run campaigns for this pipeline.")
            if lead and move_existing and (not permitted["can_move_leads"] or not permission_map[str(lead.pipeline_id)]["can_move_leads"]):
                raise CampaignInputError("You cannot move this lead between these pipelines or stages.")
            name = str(row.get(mapping["name"], "")).strip()
            email = str(row.get(mapping.get("email", ""), "")).strip()
            attrs = {item.key: (lead.attributes or {}).get(item.key, "") for item in defs} if lead else {}
            changes = {}
            for definition in defs:
                column = mapping.get(f"attr:{definition.key}")
                if column and row.get(column):
                    changes[definition.key] = _attribute_value(definition, str(row[column]))
            if lead:
                name = (name or lead.name) if update_existing else lead.name
                email = (email or lead.email) if update_existing else lead.email
                if update_existing:
                    attrs.update(changes)
            else:
                attrs.update(changes)
            if not name or len(name) > 150:
                raise CampaignInputError("Name is required and may contain at most 150 characters.")
            if email:
                try:
                    validate_email(email)
                except ValidationError as exc:
                    raise CampaignInputError("Email does not have a valid format.") from exc
            reviewed.append({
                "row": index, "phone": phone, "name": name, "email": email, "attributes": attrs,
                "attribute_changes": changes if (not lead or update_existing) else {},
                "pipeline_id": str(target.pk), "stage_id": str(stage.pk),
                "pipeline_label": target.name, "stage_label": stage.name,
                "existing_id": str(lead.pk) if lead else "",
                "existing_updated_at": lead.updated_at.isoformat() if lead else "",
                "values": _values(name=name, phone=phone, email=email, attributes=attrs, organization=user.organization, defs=defs),
            })
            stats["existing" if lead else "new"] += 1
        except CampaignInputError as exc:
            stats["invalid"] += 1
            errors.append({"row": index, "reason": str(exc), "kind": "invalid"})
    stats["eligible"] = len(reviewed)
    stats["errors"] = errors
    config = {"mapping": mapping, "mode": mode, "pipeline_id": str(primary.pk), "stage_id": str(default_stage.pk) if default_stage else "",
              "update_existing": update_existing, "move_existing": move_existing}
    upload.reviewed_rows, upload.review_stats, upload.review_config = reviewed, stats, config
    upload.review_digest = fingerprint({"config": config, "rows": reviewed, "counts": {key: value for key, value in stats.items() if key != "errors"}})
    upload.save(update_fields=["reviewed_rows", "review_stats", "review_config", "review_digest"])
    return upload
