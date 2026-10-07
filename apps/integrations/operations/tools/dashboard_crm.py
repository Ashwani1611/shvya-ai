"""Tenant-scoped CRM provisioning through canonical services and exact approvals."""

from copy import copy
from contextlib import nullcontext
from uuid import NAMESPACE_URL, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.accounts.models import User
from apps.accounts.phone_numbers import normalize_free_phone
from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.qualification_state import QUALIFIED_STAGE, normalize_stage_name
from apps.crm.models import AttributeDefinition, Lead, Stage
from apps.crm.services.stage_requirements import missing_attributes, required_attributes
from apps.integrations.operations.dashboard_crm_catalog import LEAD_PROPERTIES
from apps.integrations.operations.tools.lead_actions import _validate_operations_stage_move
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_LEAD_CREATE, CAP_LEAD_IMPORT, CAP_LEAD_READ, CAP_LEAD_STAGE_WRITE,
    CAP_LEAD_WRITE, CAP_ORGANIZATION_CREATE, ROLE_SUPERADMIN, approval_required,
)
from apps.integrations.operations_tools import (
    OperationsPermissionError, OperationsToolError, ToolExecution,
    _attribute_schema_snapshot, _ensure_approved_proposal_unchanged, _lead,
    _organization_for, _proposal_digest, _reject_secret_like_content,
    _require_operations_capability, _tenant_safe_leads, _uuid,
    _validated_lead_attribute_values, _write_gate,
)
from apps.organizations.models import Organization
from apps.superadmin.forms import OrganizationCreateForm
from apps.superadmin.models import AuditLog
from services.crm.attribute_service import update_lead_attribute_values
from services.crm.lead_service import create_lead
from services.crm.lead_transition import move_lead_to_pipeline_stage
from services.crm_activity_service import record_lead_updated
from services.triggers.evaluator import suppress_workflow_events


MAX_BATCH = 100


def _object(value, allowed, field):
    if not isinstance(value, dict):
        raise OperationsToolError(f"{field} must be an object.")
    if set(value) - set(allowed):
        raise OperationsToolError(f"{field} contains unsupported fields.")
    _reject_secret_like_content(value, field=field)
    return value


def _integer(value, minimum, maximum, field):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise OperationsToolError(f"{field} must be an integer from {minimum} to {maximum}.")
    return value


def _attributes(organization, values):
    if not isinstance(values, dict):
        raise OperationsToolError("attributes must be an object.")
    if "booked_at" in values:
        raise OperationsPermissionError("Booked at is managed by SHVYA Calendar. Use the explicit Calendar tools to review availability, booking changes and reminder side effects.")
    definitions = {item.key: item for item in AttributeDefinition.objects.filter(organization=organization, is_active=True)}
    if set(values) - set(definitions):
        raise OperationsToolError("Attributes must use existing active organization attribute keys.")
    if any(is_sensitive_attribute_definition({"key": key, "name": definitions[key].name}) for key in values):
        raise OperationsPermissionError("Sensitive attributes cannot be written through Operations MCP.")
    try:
        normalized = _validated_lead_attribute_values(definitions=definitions, values=values)
    except ValidationError as exc:
        raise OperationsToolError("Lead attribute validation failed.") from exc
    return normalized, _attribute_schema_snapshot(definitions=definitions, keys=values)


def _public_lead(lead, *, detail=False):
    result = {"id": str(lead.id), "name": lead.name, "phone": lead.phone, "email": lead.email,
              "pipeline_id": str(lead.pipeline_id), "pipeline": lead.pipeline.name,
              "stage_id": str(lead.stage_id), "stage": lead.stage.name,
              "lead_source": lead.lead_source, "ai_enabled": lead.ai_enabled,
              "auto_followup_enabled": lead.auto_followup_enabled,
              "updated_at": lead.updated_at.isoformat() if lead.updated_at else None}
    if detail:
        definitions = [item for item in AttributeDefinition.objects.filter(organization=lead.organization, is_active=True)
                       if not is_sensitive_attribute_definition({"key": item.key, "name": item.name})]
        result.update(notes=lead.notes, attributes={item.key: (lead.attributes or {}).get(item.key, "") for item in definitions},
                      attribute_definitions=[{"key": item.key, "name": item.name, "description": item.description,
                                              "field_type": item.field_type, "options": item.options} for item in definitions])
    return result


def list_crm_leads(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_LEAD_READ)
    limit = _integer(arguments.get("limit", 50), 1, 100, "limit")
    offset = _integer(arguments.get("offset", 0), 0, 100000, "offset")
    query = arguments.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise OperationsToolError("query must be a string of at most 200 characters.")
    qs = _tenant_safe_leads(organization).select_related("pipeline", "stage")
    for key in ("pipeline_id", "stage_id"):
        if arguments.get(key):
            qs = qs.filter(**{key: _uuid(arguments[key], field=key)})
    if query.strip():
        qs = qs.filter(Q(name__icontains=query.strip()) | Q(phone__icontains=query.strip()) | Q(email__icontains=query.strip()))
    rows = list(qs.order_by("-created_at", "id")[offset:offset + limit + 1])
    return ToolExecution(data={"leads": [_public_lead(item) for item in rows[:limit]], "has_more": len(rows) > limit,
                               "next_offset": offset + limit if len(rows) > limit else None},
                         capability=CAP_LEAD_READ, target_type="organization", target_id=str(organization.id),
                         audit_summary={"returned_count": min(len(rows), limit)})


def get_crm_lead(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_LEAD_READ)
    lead = _lead(organization, arguments.get("lead_id"))
    return ToolExecution(data={"lead": _public_lead(lead, detail=True)}, capability=CAP_LEAD_READ,
                         target_type="lead", target_id=str(lead.id), audit_summary={"operation": "get_crm_lead"})


def _stage(organization, stage_id, pipeline_id=None):
    qs = Stage.objects.select_related("pipeline").filter(pk=_uuid(stage_id, field="stage_id"),
            pipeline__organization=organization, pipeline__is_active=True, is_active=True)
    if pipeline_id is not None:
        qs = qs.filter(pipeline_id=_uuid(pipeline_id, field="pipeline_id"))
    stage = qs.first()
    if stage is None:
        raise OperationsToolError("Active stage/pipeline not found in this organization.")
    return stage


def _stage_snapshot(stage):
    return {"id": str(stage.id), "pipeline_id": str(stage.pipeline_id), "name": stage.name,
            "description": stage.description,
            "required_attributes": sorted(str(item.id) for item in required_attributes(stage))}


def _workflow_setting(arguments):
    allow = arguments.get("allow_workflows", False)
    if not isinstance(allow, bool):
        raise OperationsToolError("allow_workflows must be a JSON boolean.")
    return allow


def _creation_id(organization, arguments, index=0):
    request_id = _uuid(arguments.get("client_request_id"), field="client_request_id")
    return uuid5(organization.id, f"mcp-lead:{request_id}:{index}")


def _contact_data(organization, data, *, existing=None, stage=None, source="external_api"):
    _object(data, LEAD_PROPERTIES, "lead")
    if not data:
        raise OperationsToolError("Lead changes cannot be empty.")
    for key in ("ai_enabled", "auto_followup_enabled"):
        if key in data and not isinstance(data[key], bool):
            raise OperationsToolError(f"{key} must be a JSON boolean.")
    for key in ("name", "phone", "email", "notes", "lead_source"):
        if key in data and not isinstance(data[key], str):
            raise OperationsToolError(f"{key} must be a string.")
    if len(data.get("notes", "")) > 10000:
        raise OperationsToolError("notes must be at most 10000 characters.")
    if existing is None:
        if not {"name", "phone"} <= set(data):
            raise OperationsToolError("name and phone are required; phone may be empty for Instagram leads.")
        if normalize_stage_name(stage.name) == QUALIFIED_STAGE:
            raise OperationsPermissionError("Create leads before qualification; only backend-verified qualification can enter Qualified.")
        lead = Lead(organization=organization, pipeline=stage.pipeline, stage=stage,
                    ai_enabled=False, auto_followup_enabled=False, lead_source=source)
    else:
        lead = copy(existing)
    attributes, schema = _attributes(organization, data.get("attributes", {}))
    for key, value in data.items():
        if key != "attributes":
            setattr(lead, key, value.strip() if isinstance(value, str) else value)
    lead.attributes = {**(lead.attributes or {}), **attributes}
    try:
        lead.full_clean()
    except ValidationError as exc:
        raise OperationsToolError("Lead validation failed: " + "; ".join(exc.messages)) from exc
    if existing is None and missing_attributes(stage, lead.attributes):
        raise OperationsToolError("Complete the target stage's required attributes before creating a lead there.")
    fields = set(data) - {"attributes"} if existing else set(LEAD_PROPERTIES) - {"attributes"}
    values = {key: getattr(lead, key) for key in sorted(fields)}
    if "attributes" in data:
        values["attributes"] = attributes
    return lead, values, schema


def _preview(identity, organization, capability, name, reason, proposal, *, target_type="organization", target_id="", risk=""):
    return ToolExecution(data={"status": "DRY_RUN", "proposed_change": proposal, "risk": risk,
                               "approval_required": approval_required(role=identity.role, organization=organization, capability=capability)},
                         capability=capability, target_type=target_type, target_id=target_id or (str(organization.id) if organization else ""),
                         reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN,
                         audit_summary={"operation": name, "proposal_digest": _proposal_digest(proposal)})


def _created_lead(organization, stage, values, *, lead_id, allow_workflows):
    values = dict(values)
    attributes = values.pop("attributes", {})
    with nullcontext() if allow_workflows else suppress_workflow_events():
        lead = create_lead(organization=organization, pipeline=stage.pipeline, stage=stage,
                           send_welcome=False, id=lead_id, **values, attributes=attributes)
    lead.refresh_from_db()
    if lead.pipeline_id != stage.pipeline_id or lead.stage_id != stage.id:
        raise OperationsToolError("Created lead routing verification failed.")
    for key, value in values.items():
        if getattr(lead, key) != value:
            raise OperationsToolError("Created lead readback verification failed.")
    return lead


def create_crm_lead(*, identity, arguments):
    organization = _organization_for(identity)
    capability = CAP_LEAD_CREATE
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=capability,
                                 tool_name="create_crm_lead", arguments=arguments)
    allow_workflows = _workflow_setting(arguments)
    lead_id = _creation_id(organization, arguments)
    with transaction.atomic():
        # Serializes retries/parallel imports in the same tenant without a global lock.
        if not dry_run:
            Organization.objects.select_for_update().get(pk=organization.pk)
        if Lead.objects.filter(organization=organization, pk=lead_id).exists():
            raise OperationsToolError(f"client_request_id already created lead {lead_id}; inspect that lead instead of retrying creation.")
        stage = _stage(organization, arguments.get("stage_id"), arguments.get("pipeline_id"))
        _, values, schema = _contact_data(organization, arguments.get("data"), stage=stage)
        proposal = {"stage": _stage_snapshot(stage), "data": values, "attribute_schema": schema,
                    "lead_id": str(lead_id), "allow_workflows": allow_workflows}
        if dry_run:
            return _preview(identity, organization, capability, "create_crm_lead", reason, proposal,
                            risk="Creates a CRM lead. Direct welcome is suppressed. Workflow events run only when allow_workflows=true.")
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        lead = _created_lead(organization, stage, values, lead_id=lead_id, allow_workflows=allow_workflows)
    return ToolExecution(data={"status": "CREATED", "lead": _public_lead(lead), "verification": "passed"},
                         capability=capability, target_type="lead", target_id=str(lead.id), reason=reason,
                         audit_summary={"operation": "create_crm_lead", "verification": "passed"})


def update_crm_lead(*, identity, arguments):
    organization = _organization_for(identity)
    capability = CAP_LEAD_WRITE
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=capability,
                                 tool_name="update_crm_lead", arguments=arguments)
    with transaction.atomic():
        qs = _tenant_safe_leads(organization).select_related("organization", "pipeline", "stage")
        if not dry_run:
            qs = qs.select_for_update()
        lead = qs.filter(pk=_uuid(arguments.get("lead_id"), field="lead_id")).first()
        if lead is None:
            raise OperationsToolError("Lead not found in this organization.")
        _, values, schema = _contact_data(organization, arguments.get("changes"), existing=lead)
        before = {key: ({k: (lead.attributes or {}).get(k) for k in values[key]} if key == "attributes" else getattr(lead, key)) for key in values}
        proposal = {"lead_id": str(lead.id), "updated_at": lead.updated_at.isoformat(),
                    "before": before, "after": values, "attribute_schema": schema}
        if dry_run:
            return _preview(identity, organization, capability, "update_crm_lead", reason, proposal,
                            target_type="lead", target_id=str(lead.id), risk="Changes contact fields and selected CRM attributes; AI/follow-up enablement can affect later automation.")
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        changed = {key: {"old": before[key], "new": value} for key, value in values.items() if before[key] != value}
        scalar = {key: value for key, value in values.items() if key != "attributes"}
        for key, value in scalar.items():
            setattr(lead, key, value)
        if scalar:
            lead.save(update_fields=[*scalar, "updated_at"])
        if "attributes" in values:
            update_lead_attribute_values(organization=organization, lead=lead, values=values["attributes"])
        if changed:
            record_lead_updated(lead=lead, actor=identity.actor, changed_fields=changed)
        lead.refresh_from_db()
        if any(getattr(lead, key) != value for key, value in scalar.items()) or any((lead.attributes or {}).get(key) != value for key, value in values.get("attributes", {}).items()):
            raise OperationsToolError("CRM lead readback verification failed.")
    return ToolExecution(data={"status": "UPDATED", "lead": _public_lead(lead), "updated_fields": sorted(changed), "verification": "passed"},
                         capability=capability, target_type="lead", target_id=str(lead.id), reason=reason,
                         audit_summary={"operation": "update_crm_lead", "updated_fields": sorted(changed), "verification": "passed"})


def import_crm_leads(*, identity, arguments):
    organization = _organization_for(identity)
    capability = CAP_LEAD_IMPORT
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=capability,
                                 tool_name="import_crm_leads", arguments=arguments)
    allow_workflows = _workflow_setting(arguments)
    _creation_id(organization, arguments)
    rows = arguments.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
        raise OperationsToolError("rows must contain 1–100 lead objects.")
    policy = arguments.get("duplicate_policy", "error")
    if policy not in {"error", "skip"}:
        raise OperationsToolError("duplicate_policy must be error or skip.")
    with transaction.atomic():
        if not dry_run:
            Organization.objects.select_for_update().get(pk=organization.pk)
        stage = _stage(organization, arguments.get("stage_id"), arguments.get("pipeline_id"))
        accepted, skipped, seen = [], [], set()
        for index, row in enumerate(rows):
            _object(row, LEAD_PROPERTIES, "row")
            lead_id = _creation_id(organization, arguments, index + 1)
            if Lead.objects.filter(organization=organization, pk=lead_id).exists():
                raise OperationsToolError("client_request_id already imported this batch; inspect prior import results before retrying.")
            from apps.crm.models.lead import normalize_phone
            try:
                phone = normalize_phone(row.get("phone")) if row.get("phone") else ""
            except ValidationError as exc:
                raise OperationsToolError(f"Row {index + 1}: country-code phone is required.") from exc
            if phone and (phone in seen or Lead.objects.filter(organization=organization, phone=phone).exists()):
                if policy == "error":
                    raise OperationsToolError(f"Row {index + 1}: duplicate phone in organization or batch.")
                skipped.append(index + 1)
                continue
            _, values, schema = _contact_data(organization, row, stage=stage, source="csv_import")
            if phone:
                seen.add(phone)
            accepted.append({"row": index + 1, "lead_id": str(lead_id), "data": values, "attribute_schema": schema})
        proposal = {"stage": _stage_snapshot(stage), "rows": accepted, "skipped_rows": skipped, "duplicate_policy": policy,
                    "allow_workflows": allow_workflows}
        if dry_run:
            return _preview(identity, organization, capability, "import_crm_leads", reason, proposal,
                            risk="Atomic bounded import. Duplicate policy never overwrites existing leads. Direct welcomes are suppressed; Workflow events run only when allow_workflows=true.")
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        leads = [_created_lead(organization, stage, item["data"], lead_id=item["lead_id"], allow_workflows=allow_workflows) for item in accepted]
    return ToolExecution(data={"status": "IMPORTED", "created_count": len(leads), "skipped_rows": skipped,
                               "lead_ids": [str(lead.id) for lead in leads], "verification": "passed"},
                         capability=capability, target_type="organization", target_id=str(organization.id), reason=reason,
                         audit_summary={"operation": "import_crm_leads", "created_count": len(leads), "skipped_count": len(skipped), "verification": "passed"})


def bulk_move_crm_leads(*, identity, arguments):
    organization = _organization_for(identity)
    capability = CAP_LEAD_STAGE_WRITE
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=capability,
                                 tool_name="bulk_move_crm_leads", arguments=arguments)
    ids = arguments.get("lead_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
        raise OperationsToolError("lead_ids must contain 1–100 unique lead IDs.")
    ids = [_uuid(item, field="lead_id") for item in ids]
    if len(set(ids)) != len(ids):
        raise OperationsToolError("lead_ids must be unique.")
    with transaction.atomic():
        stage = _stage(organization, arguments.get("target_stage_id"))
        qs = _tenant_safe_leads(organization).filter(pk__in=ids).select_related("organization", "pipeline", "stage").order_by("id")
        if not dry_run:
            qs = qs.select_for_update()
        leads = list(qs)
        if len(leads) != len(ids):
            raise OperationsToolError("Every lead must belong to the active organization with valid routing.")
        for lead in leads:
            _validate_operations_stage_move(lead=lead, stage=stage)
        proposal = {"target": _stage_snapshot(stage), "leads": [{"id": str(lead.id), "pipeline_id": str(lead.pipeline_id),
                    "stage_id": str(lead.stage_id), "updated_at": lead.updated_at.isoformat()} for lead in leads]}
        if dry_run:
            return _preview(identity, organization, capability, "bulk_move_crm_leads", reason, proposal,
                            risk="Moves exact lead IDs, records history and can run configured stage-moved Workflows.")
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        for lead in leads:
            move_lead_to_pipeline_stage(lead=lead, pipeline=stage.pipeline, stage=stage, actor=identity.actor)
            lead.refresh_from_db(fields=["pipeline", "stage"])
            if lead.pipeline_id != stage.pipeline_id or lead.stage_id != stage.id:
                raise OperationsToolError("Bulk move readback verification failed.")
    return ToolExecution(data={"status": "MOVED", "lead_ids": [str(lead.id) for lead in leads], "count": len(leads),
                               "target_stage_id": str(stage.id), "verification": "passed"},
                         capability=capability, target_type="organization", target_id=str(organization.id), reason=reason,
                         audit_summary={"operation": "bulk_move_crm_leads", "count": len(leads), "target_stage_id": str(stage.id), "verification": "passed"})


def _owner(data, organization):
    if data is None:
        return None
    _object(data, {"name", "email", "phone"}, "owner")
    if any(not isinstance(value, str) for value in data.values()):
        raise OperationsToolError("Owner fields must be strings.")
    email = User.objects.normalize_email(data.get("email", "").strip())
    if User.objects.filter(email__iexact=email).exists():
        raise OperationsToolError("Owner email already belongs to an account.")
    try:
        phone = normalize_free_phone(data.get("phone", "")) if organization.package == Organization.Package.FREE else data.get("phone", "").strip()
        user = User(organization=organization, email=email, name=data.get("name", "").strip(), phone=phone,
                    role=User.Role.ADMIN, is_active=False, is_staff=False, is_superuser=False)
        user.set_unusable_password()
        user.full_clean(exclude=["organization"])
    except ValidationError as exc:
        raise OperationsToolError("Owner validation failed: " + "; ".join(exc.messages)) from exc
    return user


def create_organization_account(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError("Only SHVYA Superadmin can create organization accounts.")
    capability = CAP_ORGANIZATION_CREATE
    dry_run, reason = _write_gate(identity=identity, organization=None, capability=capability,
                                 tool_name="create_organization_account", arguments=arguments)
    data = _object(arguments.get("data"), {"name", "package", "number_of_seats", "owner"}, "organization")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise OperationsToolError("Organization name is required.")
    seats = _integer(data.get("number_of_seats", 1), 1, 10000, "number_of_seats")
    with transaction.atomic():
        if not dry_run:
            # Platform provisioning through one actor is serialized; the form also
            # rechecks names and the database protects owner email uniqueness.
            User.objects.select_for_update().get(pk=identity.actor.pk)
        form = OrganizationCreateForm(data={"name": data["name"].strip(), "package": data.get("package", "free"),
                                           "payment_mode": "full", "number_of_seats": seats, "credits_total": 0})
        form.instance.id = uuid5(NAMESPACE_URL, "shvya-mcp-organization:" + data["name"].strip().casefold())
        if not form.is_valid():
            raise OperationsToolError("Organization validation failed: " + "; ".join(str(message) for messages in form.errors.values() for message in messages))
        owner = _owner(data.get("owner"), form.instance)
        proposal = {"name": form.cleaned_data["name"], "package": form.cleaned_data["package"], "number_of_seats": seats,
                    "credits_total": 0, "owner": ({"name": owner.name, "email": owner.email, "phone": owner.phone,
                    "role": "admin", "is_active": False, "password_setup_required": True} if owner else None),
                    "default_pipeline": "Leads", "notifications_sent": False}
        if dry_run:
            return _preview(identity, None, capability, "create_organization_account", reason, proposal, target_type="platform",
                            risk="Creates a new organization and optional inactive administrator. Requires Superadmin password setup and activation before login.")
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        try:
            organization = form.save()
            if owner:
                owner.organization = organization
                owner.full_clean()
                owner.save()
                AuditLog.record(actor=identity.actor, action=AuditLog.Action.USER_CREATED,
                                target=owner, organization_id=str(organization.id), source="operations_mcp")
            AuditLog.record(actor=identity.actor, action=AuditLog.Action.ORGANIZATION_CREATED,
                            target=organization, source="operations_mcp")
        except (ValidationError, IntegrityError) as exc:
            raise OperationsToolError("Account creation validation failed; no account changes were committed.") from exc
        organization.refresh_from_db()
        pipelines = list(organization.pipelines.values("id", "name"))
        if not any(item["name"] == "Leads" for item in pipelines):
            raise OperationsToolError("Default CRM initialization verification failed.")
        if owner:
            owner.refresh_from_db()
            if owner.organization_id != organization.id or owner.is_active or owner.has_usable_password() or owner.role != User.Role.ADMIN:
                raise OperationsToolError("Owner provisioning verification failed.")
    return ToolExecution(data={"status": "CREATED", "organization_id": str(organization.id), "name": organization.name,
                               "pipelines": [{"id": str(item["id"]), "name": item["name"]} for item in pipelines],
                               "owner_id": str(owner.id) if owner else None, "password_setup_required": bool(owner),
                               "activation_required": bool(owner), "notifications_sent": False,
                               "next_action": "Select the new organization context. If an owner was supplied, Superadmin must set its password and activate the user in the organization console.",
                               "verification": "passed"},
                         capability=capability, target_type="platform", target_id=str(organization.id), reason=reason,
                         audit_summary={"operation": "create_organization_account", "organization_id": str(organization.id),
                                        "owner_id": str(owner.id) if owner else None, "verification": "passed"})


DASHBOARD_CRM_HANDLERS = {function.__name__: function for function in (
    list_crm_leads, get_crm_lead, create_crm_lead, update_crm_lead,
    import_crm_leads, bulk_move_crm_leads, create_organization_account,
)}
