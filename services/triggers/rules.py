"""Tenant-scoped rule authoring and canonical validation."""

import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Lead, LeadCall, Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.organizations.models import Organization
from apps.triggers.constants import ACTIONS, TRIGGERS
from apps.triggers.models import SmartTrigger


def fail(message):
    raise ValidationError(message)


def source_choices():
    """The built-in creation origin is not a tenant-editable custom attribute."""
    return dict(Lead._meta.get_field("lead_source").choices)


def catalog(org):
    return {
        "triggers": TRIGGERS,
        "actions": ACTIONS,
        "sources": source_choices(),
        "pipelines": list(
            Pipeline.objects.filter(organization=org, is_active=True).values(
                "id", "name"
            )
        ),
        "stages": list(
            Stage.objects.filter(
                pipeline__organization=org, pipeline__is_active=True, is_active=True
            ).values("id", "pipeline_id", "name")
        ),
        "sequences": list(
            FollowupSequence.objects.filter(
                organization=org, is_active=True, whatsapp_account__organization=org
            ).values(
                "id", "name"
            )
        ),
        "attributes": list(
            AttributeDefinition.objects.filter(organization=org).values(
                "key", "name", "field_type", "options"
            )
        ),
        "accounts": list(
            WhatsAppAccount.objects.filter(
                organization=org, is_active=True, status="connected",
                connection_type__in=[WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted],
            ).values("id", "business_name", "display_phone_number")
        ),
        "call_statuses": dict(LeadCall._meta.get_field("status").choices),
        "timezone": org.timezone,
    }


def duration(data, *, positive=False):
    value = data.get("duration")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < int(positive)
        or value > 525600
    ):
        fail(
            "Enter a whole-number duration within 0–525600 (at least 1 for a trigger)."
        )
    if data.get("unit") not in ("minutes", "hours", "days"):
        fail("Choose minutes, hours, or days.")
    return {"duration": value, "unit": data["unit"]}


def attribute_value(definition, value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        fail("Enter a valid attribute value.")
    value = str(value).strip()
    try:
        if definition.field_type == "numeric":
            if not Decimal(value).is_finite():
                raise ValueError
        elif definition.field_type == "date":
            date.fromisoformat(value)
        elif definition.field_type == "datetime":
            datetime.fromisoformat(value)
        elif definition.field_type == "option" and value not in definition.options:
            raise ValueError
    except (ValueError, InvalidOperation):
        fail("The value must match the CRM attribute type and available options.")
    return value


def validate(org, data):
    if not isinstance(data, dict):
        fail("Expected a rule object.")
    name = data.get("name", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
        fail("Rule name is required and must be at most 255 characters.")
    kind, action_type = data.get("trigger_type"), data.get("action_type")
    if (
        not isinstance(kind, str)
        or not isinstance(action_type, str)
        or kind not in TRIGGERS
        or action_type not in ACTIONS
    ):
        fail("Choose a supported trigger and action.")
    c, a = data.get("conditions", {}), data.get("action", {})
    if not isinstance(c, dict) or not isinstance(a, dict):
        fail("Conditions and action must be objects.")
    attrs = {x.key: x for x in AttributeDefinition.objects.filter(organization=org)}

    def attribute(key):
        if not isinstance(key, str) or key not in attrs:
            fail("Choose an existing CRM attribute.")
        return attrs[key]

    def stage_pair(pair, multiple=False):
        if not isinstance(pair, dict):
            fail("Choose a pipeline and stage.")
        pipeline = str(pair.get("pipeline", ""))
        ids = pair.get("stages", []) if multiple else [pair.get("stage")]
        if not isinstance(ids, list) or not ids or len(ids) > 200:
            fail("Select at least one stage for each pipeline.")
        ids = sorted({str(x) for x in ids})
        try:
            count = Stage.objects.filter(
                id__in=ids,
                pipeline_id=pipeline,
                pipeline__organization=org,
                pipeline__is_active=True,
                is_active=True,
            ).count()
        except (ValidationError, ValueError):
            fail("Invalid pipeline or stage.")
        if count != len(ids):
            fail("Stages must belong to the selected pipeline and organization.")
        return (
            {"pipeline": pipeline, "stages": ids}
            if multiple
            else {"pipeline": pipeline, "stage": ids[0]}
        )

    scopes = c.get("scopes", [])
    if (
        not isinstance(scopes, list)
        or len(scopes) > 100
        or (not scopes and kind != "sequence_ended")
    ):
        fail("Choose at least one pipeline and stage.")
    scopes = [stage_pair(x, True) for x in scopes]
    merged = {}
    for scope in scopes:
        merged.setdefault(scope["pipeline"], set()).update(scope["stages"])
    clean_c = {
        "scopes": [
            {"pipeline": p, "stages": sorted(s)} for p, s in sorted(merged.items())
        ],
        "attributes": [],
    }
    sources = c.get("sources", [])
    allowed_sources = source_choices()
    if (
        not isinstance(sources, list)
        or len(sources) > len(allowed_sources)
        or not all(isinstance(value, str) and value in allowed_sources for value in sources)
    ):
        fail("Choose valid lead creation sources.")
    # Omitting an unrestricted source filter preserves existing fingerprints.
    if sources:
        clean_c["sources"] = sorted(set(sources))
    conditions = c.get("attributes", [])
    if not isinstance(conditions, list) or len(conditions) > 50:
        fail("Use at most 50 attribute conditions.")
    for cond in conditions:
        if not isinstance(cond, dict):
            fail("Invalid attribute condition.")
        definition = attribute(cond.get("key"))
        values = cond.get("values")
        if (
            cond.get("match") not in ("equals", "contains")
            or not isinstance(values, list)
            or not values
            or len(values) > 50
        ):
            fail("Select Equals or Contains and enter at least one value.")
        if not all(isinstance(x, str) and x.strip() for x in values):
            fail("Condition values cannot be empty.")
        clean_c["attributes"].append(
            {
                "key": definition.key,
                "match": cond["match"],
                "values": sorted({x.strip().casefold() for x in values}),
            }
        )
    clean_c["attributes"].sort(key=lambda x: json.dumps(x, sort_keys=True))
    if kind in ("no_response", "stage_idle"):
        clean_c.update(duration(c, positive=True))
    if kind == "keyword":
        keywords = c.get("keywords", [])
        if (
            not isinstance(keywords, list)
            or not 1 <= len(keywords) <= 50
            or not all(isinstance(k, str) and k.strip() for k in keywords)
            or len(",".join(keywords)) > 500
        ):
            fail("Enter 1–50 keywords, at most 500 characters; * matches any message.")
        clean_c["keywords"] = sorted({k.strip().casefold() for k in keywords})
    if kind == "call_logged":
        if not isinstance(c.get("call_status"), str) or c.get(
            "call_status"
        ) not in dict(LeadCall._meta.get_field("status").choices):
            fail("Choose a CRM call status.")
        clean_c["call_status"] = c["call_status"]

    def sequences(ids):
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            fail("Choose between 1 and 100 sequences.")
        ids = sorted({str(x) for x in ids})
        try:
            count = FollowupSequence.objects.filter(
                organization=org, is_active=True, id__in=ids,
                whatsapp_account__organization=org,
            ).count()
        except (ValidationError, ValueError):
            fail("Invalid sequence.")
        if count != len(ids):
            fail("Choose existing sequences from your organization.")
        return ids

    if kind == "sequence_ended":
        clean_c["sequences"] = sequences(c.get("sequences"))
    clean_a = {}
    if action_type == "start_sequence":
        clean_a = {
            "sequence": sequences([a.get("sequence")])[0],
            "replace": a.get("replace", False),
        }
    elif action_type == "move_stage":
        clean_a = stage_pair(a)
    elif action_type in ("ai", "followup"):
        clean_a = {"enabled": a.get("enabled")}
    elif action_type == "attribute":
        definition = attribute(a.get("key"))
        clean_a = {
            "key": definition.key,
            "value": attribute_value(definition, a.get("value")),
        }
    elif action_type in ("email", "message", "reminder"):
        if action_type in ("email", "message"):
            if (
                not isinstance(a.get("body"), str)
                or not a["body"].strip()
                or len(a["body"]) > 20000
            ):
                fail("Enter a message body (up to 20000 characters).")
            clean_a["body"] = a["body"].strip()
        if action_type == "email":
            subject = a.get("subject", "")
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 255
                or "\n" in subject
                or "\r" in subject
            ):
                fail("Enter a subject of up to 255 characters on one line.")
            clean_a.update(subject=subject.strip(), recipient="lead")
        elif action_type == "reminder":
            clean_a.update(duration(a))
            note = a.get("note", "")
            if not isinstance(note, str) or len(note) > 2000:
                fail("Reminder note must be at most 2000 characters.")
            clean_a.update(note=note, overwrite=a.get("overwrite", False))
        else:
            try:
                valid = WhatsAppAccount.objects.filter(
                    organization=org,
                    id=a.get("account"),
                    is_active=True,
                    status="connected",
                    connection_type__in=[WhatsAppAccount.ConnectionType.API, WhatsAppAccount.ConnectionType.coexisted],
                ).exists()
            except (ValidationError, ValueError):
                valid = False
            if not valid:
                fail("Select a connected WhatsApp account from your organization.")
            clean_a.update(account=str(a["account"]), schedule=a.get("schedule"))
            if a.get("schedule") == "relative":
                clean_a.update(duration(a))
            elif a.get("schedule") == "fixed":
                try:
                    clock = time.fromisoformat(a.get("time", ""))
                    if clock.tzinfo is not None:
                        raise ValueError
                    clean_a["time"] = clock.strftime("%H:%M")
                except (ValueError, TypeError):
                    fail("Choose a time of day.")
            elif a.get("schedule") == "attribute":
                definition = attribute(a.get("date_attribute"))
                if definition.field_type != "datetime":
                    fail("Choose a date-time attribute.")
                clean_a["date_attribute"] = definition.key
            else:
                fail("Choose when to send the message.")
    for key in ("enabled", "replace", "overwrite"):
        if key in clean_a and not isinstance(clean_a[key], bool):
            fail("Choose On or Off for the selected action.")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        fail("Enabled must be true or false.")
    canonical = {
        "trigger_type": kind,
        "conditions": clean_c,
        "action_type": action_type,
        "action": clean_a,
    }
    return dict(
        canonical,
        name=name.strip(),
        enabled=enabled,
        fingerprint=hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode()
        ).hexdigest(),
    )


@transaction.atomic
def save_rule(user, data, rule_id=None):
    if user.role != "admin":
        fail("Only admins can edit rules.")
    Organization.objects.select_for_update().get(id=user.organization_id)
    rule = (
        SmartTrigger.objects.get(id=rule_id, organization=user.organization)
        if rule_id
        else None
    )
    if rule and isinstance(data, dict) and set(data) == {"enabled"}:
        if not isinstance(data["enabled"], bool):
            fail("Enabled must be true or false.")
        if data["enabled"]:
            # Re-enable only a valid workflow, but always permit a safe stop.
            validate(user.organization, {
                "name": rule.name, "enabled": True,
                "trigger_type": rule.trigger_type, "conditions": rule.conditions,
                "action_type": rule.action_type, "action": rule.action,
            })
        rule.enabled = data["enabled"]
        rule.save(update_fields=["enabled", "updated_at"])
        return rule
    values = validate(user.organization, data)
    duplicate = SmartTrigger.objects.filter(
        organization=user.organization, fingerprint=values["fingerprint"]
    )
    if rule:
        duplicate = duplicate.exclude(id=rule.id)
    if duplicate.exists():
        fail("An identical rule already exists. Change its conditions or action.")
    if not rule:
        position = (
            SmartTrigger.objects.filter(organization=user.organization).aggregate(
                n=Max("position")
            )["n"]
            or 0
        ) + 1
        rule = SmartTrigger(
            organization=user.organization, created_by=user, position=position
        )
    for key, value in values.items():
        setattr(rule, key, value)
    rule.save()
    return rule


@transaction.atomic
def reorder(user, ids):
    if user.role != "admin":
        fail("Only admins can reorder rules.")
    Organization.objects.select_for_update().get(id=user.organization_id)
    rules = list(SmartTrigger.objects.filter(organization=user.organization))
    if (
        not isinstance(ids, list)
        or not all(isinstance(item, str) for item in ids)
        or len(ids) != len(rules)
        or set(ids) != {str(r.id) for r in rules}
    ):
        fail("The list changed. Refresh and try again.")
    for rule in rules:
        rule.position = ids.index(str(rule.id))
    SmartTrigger.objects.bulk_update(rules, ["position"])
