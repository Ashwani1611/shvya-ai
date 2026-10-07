"""Tenant-scoped channel authoring and explicitly approved operations group messages.

Authoring never sends or enrolls leads. Execution remains with SHVYA's normal
Cadence dispatchers, including sender routing, suppression and channel windows.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from apps.accounts.models import User
from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.hosted_gateway_routing import gateway_client_for_account
from apps.channels.instagram_models import InstagramAccount
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.followups.models import FollowupSequence
from apps.integrations.diagnostic_auth import sanitize_data
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_CADENCE_CONFIG_WRITE,
    CAP_CHANNEL_GROUP_READ,
    CAP_CHANNEL_GROUP_SEND,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired, OperationsToolError, ToolExecution,
    _ensure_approved_proposal_unchanged, _organization_for, _proposal_digest,
    _reject_secret_like_content, _require_operations_capability, _uuid,
    _validated_approval_event, _write_gate,
)
from apps.organizations.models import Organization
from apps.teams.services.whatsapp_connections import member_whatsapp_connections
from services.channels.campaign_policy import CampaignInputError, template_fields
from services.channels.hosted_automation_service import (
    HostedAutomationError, add_hosted_whatsapp_step,
)
from services.channels.template_rendering import delivery_spec, default_delivery_bindings, validate_delivery_bindings
from services.channels.template_service import TemplateError, available_placeholders
from services.followup_service import (
    FollowupError, add_email_step, add_reminder_step, add_whatsapp_step,
    create_sequence, update_sequence,
)

CHANNELS = ("api", "coexistence", "hosted", "instagram")
GROUP_ID_RE = re.compile(r"^[0-9]+(?:-[0-9]+)?@g\.us$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def account_channel(account):
    """Keep Coexistence provenance distinct from the legacy Hosted enum."""
    if isinstance(account, InstagramAccount):
        return "instagram"
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return "hosted"
    latest = WhatsAppConnectionAttempt.objects.filter(
        organization_id=account.organization_id, account=account,
        status=WhatsAppConnectionAttempt.Status.CONNECTED,
    ).order_by("-updated_at", "-created_at", "-pk").values_list("stage", flat=True).first()
    if latest in {"coexistence_connected", "coexistence_sync_warning"} or WhatsAppMessage.objects.filter(
        organization_id=account.organization_id, account=account,
        media_payload__coexistence_sync=True,
    ).exists():
        return "coexistence"
    return "api"


def _read(identity, capability=CAP_ORGANIZATION_READ):
    org = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=org, capability=capability)
    return org


def _result(*, org, capability, data, reason="", dry_run=False, proposal=None,
            target_type="organization", target_id=None):
    return ToolExecution(
        data=data, capability=capability, target_type=target_type,
        target_id=str(target_id or org.pk), reason=reason,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN if dry_run else OperationsAuditEvent.Outcome.SUCCESS,
        audit_summary={"proposal_digest": _proposal_digest(proposal)} if proposal is not None else {},
    )


def _account(org, channel, account_id, *, lock=False):
    if channel not in CHANNELS:
        raise OperationsToolError("channel must be api, coexistence, hosted, or instagram.")
    model = InstagramAccount if channel == "instagram" else WhatsAppAccount
    query = model.objects.filter(pk=_uuid(account_id, field="account_id"), organization=org)
    if lock:
        query = query.select_for_update()
    obj = query.defer("access_token").first()
    if obj is None or (channel != "instagram" and not obj.is_active):
        raise OperationsToolError("Active channel account not found in this organization.")
    if account_channel(obj) != channel:
        raise OperationsToolError("Selected account does not match the exact requested channel.")
    if channel != "hosted" and obj.status != "connected":
        raise OperationsToolError("The selected channel account must be connected.")
    return obj


def _cadence(org, cadence_id, *, lock=False):
    query = FollowupSequence.objects.filter(pk=_uuid(cadence_id, field="cadence_id"), organization=org)
    if lock:
        query = query.select_for_update(of=("self",))
    obj = query.select_related("whatsapp_account", "instagram_account").defer(
        "whatsapp_account__access_token", "instagram_account__access_token",
    ).first()
    if obj is None:
        raise OperationsToolError("Cadence not found in this organization.")
    sender = obj.instagram_account if obj.instagram_account_id else obj.whatsapp_account
    if sender is None or sender.organization_id != org.id:
        raise OperationsToolError("Cadence has an invalid cross-organization sender reference.")
    return obj


def _cadence_snapshot(obj):
    account = obj.instagram_account if obj.instagram_account_id else obj.whatsapp_account
    return {"id": str(obj.id), "name": obj.name, "description": obj.description,
            "is_active": obj.is_active, "channel": account_channel(account), "account_id": str(account.id)}


def get_channel_cadence_configuration(*, identity, arguments):
    from apps.integrations.operations.tools.cadence import _step_snapshot

    org = _read(identity)
    seq = _cadence(org, arguments.get("cadence_id"))
    account = seq.instagram_account if seq.instagram_account_id else seq.whatsapp_account
    if account.organization_id != org.id:
        raise OperationsToolError("Cadence has an invalid cross-organization sender reference.")
    offset = arguments.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
        raise OperationsToolError("offset must be an integer from 0 to 100000.")
    limit = _limit(arguments)
    steps = seq.steps.select_related("sequence__whatsapp_account", "whatsapp_template").order_by("position", "id")
    count = steps.count()
    snapshots = []
    for step in steps[offset:offset + limit]:
        if step.whatsapp_template_id and (step.whatsapp_template.organization_id != org.id or step.whatsapp_template.account_id != seq.whatsapp_account_id):
            raise OperationsToolError("Cadence has an invalid template/sender reference.")
        snapshot = _step_snapshot(step)
        truncated = []
        for field, value in snapshot.items():
            if isinstance(value, str) and len(value) > 10000:
                snapshot[field] = value[:10000]
                truncated.append(field)
        snapshot["content_truncated_fields"] = truncated
        snapshots.append(snapshot)
    return _result(org=org, capability=CAP_ORGANIZATION_READ, target_type="cadence", target_id=seq.pk,
                   data={"cadence": _cadence_snapshot(seq), "steps": snapshots, "total_steps": count,
                         "next_offset": offset + limit if count > offset + limit else None})


def _text(org, text, *, max_length=10000):
    if not isinstance(text, str) or not text.strip() or len(text) > max_length:
        raise OperationsToolError(f"Message text must contain 1–{max_length} characters.")
    allowed = {row["key"] for row in available_placeholders(organization=org)}
    unknown = set(PLACEHOLDER_RE.findall(text)) - allowed
    remainder = PLACEHOLDER_RE.sub("", text)
    if unknown or "{{" in remainder or "}}" in remainder:
        raise OperationsToolError("Unknown or malformed CRM placeholder. Use get_channel_authoring_schema.")
    # Runtime text renderers use canonical no-space braces.
    return PLACEHOLDER_RE.sub(lambda m: "{{" + m.group(1) + "}}", text.strip())


def get_channel_authoring_schema(*, identity, arguments):
    org = _read(identity)
    accounts = []
    for obj in WhatsAppAccount.objects.filter(organization=org, is_active=True).defer("access_token").order_by("id")[:100]:
        accounts.append({"id": str(obj.id), "channel": account_channel(obj), "name": obj.business_name,
                         "number": obj.display_phone_number, "status": obj.status})
    for obj in InstagramAccount.objects.filter(organization=org).defer("access_token")[:100]:
        accounts.append({"id": str(obj.id), "channel": "instagram", "name": obj.username, "status": obj.status})
    data = {
        "accounts": accounts, "placeholders": available_placeholders(organization=org),
        "placeholder_syntax": "{{lead_name}}", "channels": {
            "api": {"message_type": "template", "approved_template_required": True},
            "coexistence": {"message_type": "template", "approved_template_required": True, "transport": "meta_cloud_api"},
            "hosted": {"message_type": "text", "transport": "linked_device"},
            "instagram": {"message_type": "text", "max_characters": 1000, "requires_existing_conversation": True},
        }, "execution_guards": ["current pipeline sender", "opt-out and handoff suppression", "channel messaging window"],
        "authoring_sends_messages": False,
    }
    if arguments.get("template_id"):
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(arguments["template_id"], field="template_id"), organization=org,
            account__organization=org, account__connection_type="api",
        ).first()
        if template is None:
            raise OperationsToolError("Template not found in this organization.")
        try:
            spec, state = _template_spec(template)
            data["template_delivery"] = {"template_id": str(template.id), "fields": template_fields(spec),
                                         "bindings": state.delivery_bindings or {}, "media_defaults": spec["media_defaults"]}
        except (TemplateError, CampaignInputError) as exc:
            raise OperationsToolError(str(exc)) from exc
    return _result(org=org, capability=CAP_ORGANIZATION_READ, data=data)


def upsert_channel_cadence(*, identity, arguments):
    org = _organization_for(identity)
    dry, reason = _write_gate(identity=identity, organization=org, capability=CAP_CADENCE_CONFIG_WRITE,
                              tool_name="upsert_channel_cadence", arguments=arguments)
    data = arguments.get("data")
    if not isinstance(data, dict) or set(data) - {"name", "description", "channel", "account_id", "is_active"}:
        raise OperationsToolError("data contains unsupported Cadence fields.")
    _reject_secret_like_content(data, field="cadence")

    def resolve(lock=False):
        seq = _cadence(org, arguments["cadence_id"], lock=lock) if arguments.get("cadence_id") else None
        before = _cadence_snapshot(seq) if seq else None
        channel = data.get("channel", before["channel"] if before else "")
        account_id = data.get("account_id", before["account_id"] if before else "")
        account = _account(org, channel, account_id, lock=lock)
        name = str(data.get("name", seq.name if seq else "")).strip()
        desc = str(data.get("description", seq.description if seq else "")).strip()
        active = data.get("is_active", seq.is_active if seq else True)
        if not name or len(name) > 255 or len(desc) > 300 or not isinstance(active, bool):
            raise OperationsToolError("Cadence name/description or is_active is invalid.")
        if before and (before["channel"] != channel or before["account_id"] != str(account.id)):
            raise OperationsToolError("Create a new Cadence to change its sender or channel.")
        duplicates = FollowupSequence.objects.filter(organization=org, name__iexact=name)
        if seq:
            duplicates = duplicates.exclude(pk=seq.pk)
        if duplicates.exists():
            raise OperationsToolError("A Cadence with this name already exists.")
        after = {"name": name, "description": desc, "channel": channel, "account_id": str(account.id), "is_active": active}
        return seq, account, {"before": before, "after": after, "organization_id": str(org.id)}

    seq, account, proposal = resolve()
    if dry:
        return _result(org=org, capability=CAP_CADENCE_CONFIG_WRITE, reason=reason, dry_run=True, proposal=proposal,
                       data={"status": "DRY_RUN", **proposal, "outbound_messages": 0})
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=org.pk)
        seq, account, locked = resolve(lock=True)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked)
        values = locked["after"]
        try:
            if seq:
                seq = update_sequence(sequence=seq, name=values["name"], description=values["description"])
            else:
                seq = create_sequence(organization=org, created_by=identity.actor, name=values["name"],
                                      description=values["description"], whatsapp_account=None if values["channel"] == "instagram" else account,
                                      provider="api" if values["channel"] == "coexistence" else values["channel"])
            seq.is_active = values["is_active"]
            seq.save(update_fields=["is_active", "updated_at"])
            seq.refresh_from_db()
            snapshot = _cadence_snapshot(seq)
            if any(snapshot[k] != v for k, v in values.items()):
                raise OperationsToolError("Cadence readback failed; changes rolled back.")
        except FollowupError as exc:
            raise OperationsToolError(str(exc)) from exc
    return _result(org=org, capability=CAP_CADENCE_CONFIG_WRITE, reason=reason, target_type="cadence", target_id=seq.pk,
                   data={"status": "FIXED", "cadence": snapshot, "verification": "passed", "outbound_messages": 0})


def _step_plan(org, seq, data):
    from apps.integrations.operations.tools.cadence_config import _cadence_schedule
    from apps.followups.instagram import validate_body

    channel = _cadence_snapshot(seq)["channel"]
    kind = data.get("type", "template" if channel in {"api", "coexistence"} else "text")
    if kind not in {"template", "text", "email", "reminder"}:
        raise OperationsToolError("Step type must be template, text, email, or reminder.")
    schedule = _cadence_schedule(data)
    title = str(data.get("title") or "").strip()
    if len(title) > 255:
        raise OperationsToolError("Step title must be at most 255 characters.")
    template = None
    plan = {"cadence_id": str(seq.id), "sender": _cadence_snapshot(seq), "type": kind, "schedule": schedule,
            "next_position": (seq.steps.aggregate(value=Max("position"))["value"] or 0) + 1,
            "title": title, "body": "", "subject": "", "template": None, "retry_count": 0}
    if kind == "template":
        if channel not in {"api", "coexistence"}:
            raise OperationsToolError("Meta templates are only valid for API or Coexistence Cadence.")
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(data.get("template_id"), field="template_id"), organization=org,
            account_id=seq.whatsapp_account_id, status=WhatsAppTemplate.Status.APPROVED,
        ).first()
        if template is None or not template.meta_template_id:
            raise OperationsToolError("Choose an approved Meta template belonging to the Cadence sender.")
        spec, state = _template_spec(template)
        allowed = {r["key"] for r in available_placeholders(organization=org)}
        bindings = default_delivery_bindings(template, spec, allowed)
        bindings.update(validate_delivery_bindings(spec, state.delivery_bindings or {}, allowed))
        validate_delivery_bindings(spec, bindings, allowed)
        for field in template_fields(spec):
            if field["kind"] in {"image", "video", "document"} and not spec["media_defaults"].get(field["key"]):
                raise OperationsToolError("Configure a reusable template delivery attachment before adding this step.")
        retry = data.get("retry_count", 0)
        if isinstance(retry, bool) or not isinstance(retry, int) or not 0 <= retry <= 5:
            raise OperationsToolError("retry_count must be an integer from 0 to 5.")
        plan.update(template={"id": str(template.id), "meta_id": template.meta_template_id,
                              "spec": spec, "bindings": state.delivery_bindings or {}}, retry_count=retry)
    elif kind == "text":
        if channel not in {"hosted", "instagram"}:
            raise OperationsToolError("API and Coexistence Cadence require approved templates.")
        plan["body"] = _text(org, data.get("body"), max_length=1000 if channel == "instagram" else 10000)
        if channel == "hosted" and not title:
            raise OperationsToolError("Hosted message title is required.")
        if channel == "instagram":
            validate_body(plan["body"])
    elif kind == "email":
        plan.update(body=_text(org, data.get("body")), subject=_text(org, data.get("subject"), max_length=255))
    else:
        plan["body"] = _text(org, data.get("text"))
    return plan, template


def add_channel_cadence_step(*, identity, arguments):
    org = _organization_for(identity)
    dry, reason = _write_gate(identity=identity, organization=org, capability=CAP_CADENCE_CONFIG_WRITE,
                              tool_name="add_channel_cadence_step", arguments=arguments)
    data = arguments.get("data")
    if not isinstance(data, dict) or set(data) - {"type", "title", "body", "subject", "text", "template_id", "schedule", "retry_count"}:
        raise OperationsToolError("data contains unsupported Cadence step fields; use existing attachment tools for media.")
    _reject_secret_like_content(data, field="cadence_step")
    try:
        seq = _cadence(org, arguments.get("cadence_id"))
        plan, template = _step_plan(org, seq, data)
        if dry:
            return _result(org=org, capability=CAP_CADENCE_CONFIG_WRITE, reason=reason, dry_run=True, proposal=plan,
                           data={"status": "DRY_RUN", "step": plan, "outbound_messages": 0})
        with transaction.atomic():
            seq = _cadence(org, seq.pk, lock=True)
            plan, template = _step_plan(org, seq, data)
            _ensure_approved_proposal_unchanged(arguments=arguments, proposal=plan)
            options = {"sequence": seq, **plan["schedule"]}
            if plan["type"] == "template":
                step = add_whatsapp_step(template=template, retry_count=plan["retry_count"], **options)
            elif plan["type"] == "text" and plan["sender"]["channel"] == "instagram":
                from apps.followups.instagram import add_step
                step = add_step(title=plan["title"], body=plan["body"], **options)
            elif plan["type"] == "text":
                step = add_hosted_whatsapp_step(title=plan["title"], body=plan["body"], **options)
            elif plan["type"] == "email":
                step = add_email_step(title=plan["title"], subject=plan["subject"], body=plan["body"], **options)
            else:
                step = add_reminder_step(text=plan["body"], **options)
            step.refresh_from_db()
            if step.position != plan["next_position"] or step.sequence_id != seq.id:
                raise OperationsToolError("Cadence step readback failed; changes rolled back.")
    except (FollowupError, HostedAutomationError, TemplateError, CampaignInputError) as exc:
        raise OperationsToolError(str(exc)) from exc
    return _result(org=org, capability=CAP_CADENCE_CONFIG_WRITE, reason=reason, target_type="cadence_step", target_id=step.pk,
                   data={"status": "FIXED", "step_id": str(step.pk), "position": step.position,
                         "channel": plan["sender"]["channel"], "verification": "passed", "outbound_messages": 0})


def _template_spec(template):
    state = WhatsAppTemplateMetadata.objects.filter(template=template).first()
    if state is None:
        state = SimpleNamespace(components=[], placeholder_mapping={}, delivery_bindings={}, delivery_media={}, carousel_config={})
    return delivery_spec(template, state), state


def configure_whatsapp_template_delivery(*, identity, arguments):
    org = _organization_for(identity)
    dry, reason = _write_gate(identity=identity, organization=org, capability=CAP_MESSAGING_CONFIG_WRITE,
                              tool_name="configure_whatsapp_template_delivery", arguments=arguments)
    def resolve(lock=False):
        query = WhatsAppTemplate.objects.filter(pk=_uuid(arguments.get("template_id"), field="template_id"),
                                                organization=org, account__organization=org, account__connection_type="api")
        if lock:
            query = query.select_for_update(of=("self",))
        template = query.select_related("account").first()
        if template is None:
            raise OperationsToolError("API or Coexistence template not found in this organization.")
        spec, state = _template_spec(template)
        allowed = {r["key"] for r in available_placeholders(organization=org)}
        bindings = validate_delivery_bindings(spec, arguments.get("bindings"), allowed)
        required = {r["key"] for r in template_fields(spec) if r["kind"] == "text"}
        if required != set(bindings):
            raise OperationsToolError("Provide explicit CRM source or fallback bindings for every text parameter.")
        proposal = {"template_id": str(template.id), "account_id": str(template.account_id),
                    "spec": spec, "before": state.delivery_bindings or {}, "after": bindings}
        return template, proposal
    try:
        template, proposal = resolve()
        if dry:
            return _result(org=org, capability=CAP_MESSAGING_CONFIG_WRITE, reason=reason, dry_run=True, proposal=proposal,
                           data={"status": "DRY_RUN", **proposal, "outbound_messages": 0})
        with transaction.atomic():
            template, proposal = resolve(lock=True)
            _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
            state, _ = WhatsAppTemplateMetadata.objects.get_or_create(template=template)
            state.delivery_bindings = proposal["after"]
            state.save(update_fields=["delivery_bindings", "updated_at"])
            state.refresh_from_db()
            if state.delivery_bindings != proposal["after"]:
                raise OperationsToolError("Template binding readback failed.")
    except (TemplateError, CampaignInputError) as exc:
        raise OperationsToolError(str(exc)) from exc
    return _result(org=org, capability=CAP_MESSAGING_CONFIG_WRITE, reason=reason, target_type="whatsapp_template", target_id=template.pk,
                   data={"status": "FIXED", "template_id": str(template.id), "bindings": state.delivery_bindings,
                         "verification": "passed", "outbound_messages": 0})


def _group_id(value):
    if not isinstance(value, str) or not GROUP_ID_RE.fullmatch(value) or len(value) > 80:
        raise OperationsToolError("group_id must be the exact WhatsApp group ID ending @g.us, never a name or phone number.")
    return value


def _limit(arguments):
    value = arguments.get("limit", 50)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise OperationsToolError("limit must be an integer from 1 to 100.")
    return value


def list_hosted_whatsapp_groups(*, identity, arguments):
    org = _read(identity, CAP_CHANNEL_GROUP_READ)
    account = _account(org, "hosted", arguments.get("whatsapp_account_id"))
    result = gateway_client_for_account(account).list_groups(session_id=account.id, limit=_limit(arguments))
    return _result(org=org, capability=CAP_CHANNEL_GROUP_READ,
                   data={"account_id": str(account.id), "result": sanitize_data(result), "read_only": True})


def read_hosted_whatsapp_group(*, identity, arguments):
    org = _read(identity, CAP_CHANNEL_GROUP_READ)
    account = _account(org, "hosted", arguments.get("whatsapp_account_id"))
    group_id = _group_id(arguments.get("group_id"))
    result = gateway_client_for_account(account).read_group(session_id=account.id, group_id=group_id, limit=_limit(arguments))
    return _result(org=org, capability=CAP_CHANNEL_GROUP_READ,
                   data={"account_id": str(account.id), "group_id": group_id,
                         "result": sanitize_data(result, text_limit=10000), "read_only": True})


def send_hosted_whatsapp_group_message(*, identity, arguments):
    org = _organization_for(identity)
    dry, reason = _write_gate(identity=identity, organization=org, capability=CAP_CHANNEL_GROUP_SEND,
                              tool_name="send_hosted_whatsapp_group_message", arguments=arguments)
    group_id = _group_id(arguments.get("group_id"))
    body = arguments.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > 10000:
        raise OperationsToolError("body must contain 1–10000 characters.")
    _reject_secret_like_content(body, field="group_message")
    account = _account(org, "hosted", arguments.get("whatsapp_account_id"))
    member = User.objects.filter(pk=_uuid(arguments.get("sender_member_id"), field="sender_member_id"),
                                 organization=org, is_active=True).first()
    if member is None or not member.name.strip():
        raise OperationsToolError("Choose an active organization member with a real sender name.")
    owned = member_whatsapp_connections(organization=org, members=[member])[member.pk]
    if not owned["account"] or owned["account"].id != account.id:
        raise OperationsToolError("This Hosted account is not the selected member's uniquely resolved owned sender.")
    proposal = {"organization_id": str(org.id), "account_id": str(account.id), "group_id": group_id,
                "sender_member_id": str(member.id), "sender_name": member.name, "body": f"{member.name}: {body.strip()}",
                "gateway_shard": account.hosted_gateway_shard}
    if dry:
        return _result(org=org, capability=CAP_CHANNEL_GROUP_SEND, reason=reason, dry_run=True, proposal=proposal,
                       data={"status": "DRY_RUN", "message": proposal, "approval_required": True,
                             "outbound_messages": 0, "reversible": False})
    # Group sends always require a per-message receipt, including org policies
    # that waive approval for routine configuration changes.
    if arguments.get("approved") is not True or not arguments.get("approval_event_id"):
        raise OperationsApprovalRequired("Approve this exact sender, group and message after its dry-run.")
    if not approval_required(role=identity.role, organization=org, capability=CAP_CHANNEL_GROUP_SEND):
        _validated_approval_event(identity=identity, organization=org, capability=CAP_CHANNEL_GROUP_SEND,
                                  tool_name="send_hosted_whatsapp_group_message", arguments=arguments)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if account.status != WhatsAppAccount.Status.CONNECTED:
        raise OperationsToolError("Hosted sender is not connected.")
    if str(getattr(settings, "APP_ENV", "production")).lower() == "staging":
        allowed = set(getattr(settings, "STAGING_ALLOWED_RECIPIENTS", set()) or set())
        if not getattr(settings, "OUTBOUND_MESSAGING_ENABLED", False) or group_id not in allowed:
            raise OperationsToolError("Staging group sends require this exact group ID in the outbound allowlist.")
    client = gateway_client_for_account(account)
    # Gateway verifies group identity and membership, then journals this receipt
    # as the idempotency key. It never normalizes the group to a direct recipient.
    result = client.send_group_message(session_id=account.id, group_id=group_id, body=proposal["body"],
                                       request_id=arguments["approval_event_id"])
    if not isinstance(result, dict) or not result.get("ok") or not result.get("messageId"):
        raise OperationsToolError("Provider did not confirm a message ID. Delivery is unconfirmed; inspect group history before any new send.")
    return _result(org=org, capability=CAP_CHANNEL_GROUP_SEND, reason=reason,
                   data={"status": "SENT", "message": proposal, "provider_result": sanitize_data(result)},
                   target_type="hosted_group", target_id=group_id)


CHANNEL_DASHBOARD_HANDLERS = {fn.__name__: fn for fn in (
    get_channel_authoring_schema, get_channel_cadence_configuration, upsert_channel_cadence, add_channel_cadence_step,
    configure_whatsapp_template_delivery, list_hosted_whatsapp_groups,
    read_hosted_whatsapp_group, send_hosted_whatsapp_group_message,
)}
