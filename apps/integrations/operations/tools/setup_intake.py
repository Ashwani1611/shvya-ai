"""Bounded, source-attributed onboarding intake for the Operations connector."""

from datetime import date
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Count

from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_SETUP_INTAKE_READ,
    CAP_SETUP_INTAKE_WRITE,
    approval_required,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _require_operations_capability,
    _uuid,
    _write_gate,
)
from apps.integrations.setup_models import (
    OperationsIntakeEntry,
    SETUP_INTAKE_HISTORY_LIMIT,
    SETUP_INTAKE_KINDS,
    SETUP_INTAKE_ORIGINS,
    SETUP_INTAKE_SECTIONS,
    SETUP_INTAKE_STATUSES,
)

_REQUIRED_DATA_FIELDS = frozenset({
    "section", "kind", "external_id", "origin", "source_id", "source_ref", "status", "body",
})
_DATA_FIELDS = _REQUIRED_DATA_FIELDS | {"source_date", "is_active"}
_WRITE_FIELDS = {"reason", "dry_run", "approved", "approval_event_id", "expected_revision"}
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_INLINE_MEDIA_PATTERN = re.compile(r"(?:data:[^\s,]+[;,]|-----BEGIN .*PRIVATE KEY)", re.IGNORECASE)


def _arguments(arguments, allowed):
    if not isinstance(arguments, dict):
        raise OperationsToolError("arguments must be an object.")
    if set(arguments) - allowed:
        raise OperationsToolError("Unsupported setup intake argument.")
    return arguments


def _integer(value, *, field, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise OperationsToolError(f"{field} must be an integer from {minimum} to {maximum}.")
    return value


def _boolean(value, *, field):
    if not isinstance(value, bool):
        raise OperationsToolError(f"{field} must be a boolean.")
    return value


def _choice(value, choices, *, field):
    if not isinstance(value, str) or value not in choices:
        raise OperationsToolError(f"Unsupported setup intake {field}.")
    return value


def _text(value, *, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise OperationsToolError(f"{field} must be nonempty text of at most {limit} characters.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise OperationsToolError(f"{field} contains unsupported control characters.")
    _reject_secret_like_content(value, field="setup_intake")
    if _INLINE_MEDIA_PATTERN.search(value):
        raise OperationsToolError("Intake accepts evidence summaries and source references, not embedded media.")
    for match in _URL_PATTERN.finditer(value):
        try:
            parsed = urlsplit(match.group())
        except ValueError as exc:
            raise OperationsToolError("Intake source URLs must be valid references.") from exc
        if parsed.username or parsed.password or parsed.query:
            raise OperationsToolError("Intake URLs must omit credentials and query parameters; use a stable source reference.")
    return value.strip()


def _normalized_data(data):
    if not isinstance(data, dict) or set(data) - _DATA_FIELDS or not _REQUIRED_DATA_FIELDS <= set(data):
        raise OperationsToolError("data must contain the required setup intake fields and no unsupported fields.")
    values = {
        "section": _choice(data["section"], SETUP_INTAKE_SECTIONS, field="section"),
        "kind": _choice(data["kind"], SETUP_INTAKE_KINDS, field="kind"),
        "origin": _choice(data["origin"], SETUP_INTAKE_ORIGINS, field="origin"),
        "status": _choice(data["status"], SETUP_INTAKE_STATUSES, field="status"),
        "is_active": _boolean(data.get("is_active", True), field="is_active"),
    }
    for field, limit in (("external_id", 120), ("source_id", 160), ("source_ref", 500), ("body", 12000)):
        values[field] = _text(data[field], field=field, limit=limit)
    source_date = data.get("source_date")
    if source_date is not None:
        try:
            if not isinstance(source_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
                raise ValueError
            source_date = date.fromisoformat(source_date)
        except ValueError as exc:
            raise OperationsToolError("source_date must be an ISO date (YYYY-MM-DD) or null.") from exc
    values["source_date"] = source_date
    return values


def _snapshot(entry):
    values = {field: getattr(entry, field) for field in _DATA_FIELDS}
    values["source_date"] = entry.source_date.isoformat() if entry.source_date else None
    return values


def _serialized_values(values):
    return {key: value.isoformat() if isinstance(value, date) else value for key, value in values.items()}


def _safe_snapshot(values, *, body_limit):
    result = {
        key: sanitize_text(value, limit=body_limit if key == "body" else 500)
        if isinstance(value, str) else value
        for key, value in values.items() if key in _DATA_FIELDS
    }
    result["body_truncated"] = len(str(values.get("body") or "")) > body_limit
    return result


def _serialize(entry, *, include_history=False, body_limit=4000):
    result = {
        "id": str(entry.id), **_safe_snapshot(_snapshot(entry), body_limit=body_limit),
        "revision": entry.revision,
        "previous_revision_count": entry.revision - 1,
        "retained_revision_count": min(entry.revision - 1, SETUP_INTAKE_HISTORY_LIMIT),
        "created_at": entry.created_at.isoformat(), "updated_at": entry.updated_at.isoformat(),
    }
    if include_history:
        result["history"] = [
            {"revision": item["revision"], "updated_at": item["updated_at"],
             "data": _safe_snapshot(item["data"], body_limit=4000)}
            for item in entry.history[-5:]
        ]
        result["history_truncated"] = entry.revision - 1 > len(result["history"])
    return result


def _entry(organization, entry_id, *, lock=False):
    query = OperationsIntakeEntry.objects.filter(organization=organization)
    if lock:
        query = query.select_for_update()
    entry = query.filter(pk=_uuid(entry_id, field="entry_id")).first()
    if entry is None:
        raise OperationsToolError("Setup intake entry not found in this organization.")
    return entry


def get_setup_intake(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_SETUP_INTAKE_READ)
    arguments = _arguments(arguments, {
        "entry_id", "section", "kind", "include_archived", "include_history", "limit", "cursor",
    })
    limit = _integer(arguments.get("limit", 20), field="limit", minimum=1, maximum=50)
    include_archived = _boolean(arguments.get("include_archived", False), field="include_archived")
    include_history = _boolean(arguments.get("include_history", False), field="include_history")
    entry_id = arguments.get("entry_id")
    if "entry_id" in arguments:
        entry_id = _uuid(entry_id, field="entry_id")
    if include_history and not entry_id:
        raise OperationsToolError("include_history requires one entry_id.")
    if entry_id and "cursor" in arguments:
        raise OperationsToolError("entry_id cannot be combined with cursor.")
    rows = OperationsIntakeEntry.objects.filter(organization=organization)
    if not include_archived:
        rows = rows.filter(is_active=True)
    for field, choices in (("section", SETUP_INTAKE_SECTIONS), ("kind", SETUP_INTAKE_KINDS)):
        if field in arguments:
            rows = rows.filter(**{field: _choice(arguments[field], choices, field=field)})
    section_counts = dict(rows.values("section").annotate(total=Count("id")).values_list("section", "total"))
    if entry_id:
        rows = rows.filter(pk=_uuid(entry_id, field="entry_id"))
    if "cursor" in arguments:
        rows = rows.filter(pk__gt=_uuid(arguments["cursor"], field="cursor"))
    if not include_history:
        rows = rows.defer("history")
    selected = list(rows.order_by("id")[:limit + 1])
    if entry_id and not selected:
        raise OperationsToolError("Setup intake entry not found in this organization.")
    page = selected[:limit]
    return ToolExecution(
        data={
            "entries": [_serialize(row, include_history=include_history, body_limit=12000 if entry_id else 4000) for row in page],
            "count": len(page), "next_cursor": str(page[-1].id) if len(selected) > limit else None,
            "sections": [{"section": section, "count": section_counts.get(section, 0)} for section in SETUP_INTAKE_SECTIONS],
            "content_classification": "untrusted_intake_evidence", "automatic_publication": False,
        },
        capability=CAP_SETUP_INTAKE_READ, target_type="organization", target_id=str(organization.id),
        audit_summary={"operation": "get_setup_intake", "entry_count": len(page), "history_included": include_history},
    )


def _proposal(organization, entry, values):
    return {
        "organization_id": str(organization.id), "entry_id": str(entry.id) if entry else None,
        "revision": entry.revision if entry else 0,
        "before": _snapshot(entry) if entry else None, "after": _serialized_values(values),
    }


def _check_revision(arguments, entry, *, unchanged):
    expected = arguments.get("expected_revision")
    if expected is not None:
        _integer(expected, field="expected_revision")
    if entry and not unchanged and expected != entry.revision:
        raise OperationsApprovalRequired("Setup intake revision changed or expected_revision is missing. Read the entry and run a fresh dry-run.")
    if not entry and expected not in (None, 0):
        raise OperationsApprovalRequired("A new setup intake entry requires expected_revision=0 or no expected revision.")


def _resolve_upsert(organization, arguments, values, *, lock=False):
    if "entry_id" in arguments:
        entry = _entry(organization, arguments["entry_id"], lock=lock)
        if any(getattr(entry, field) != values[field] for field in ("section", "kind", "external_id")):
            raise OperationsToolError("An existing entry's section, kind and external_id cannot change; create a separate entry.")
    else:
        query = OperationsIntakeEntry.objects.filter(
            organization=organization,
            **{field: values[field] for field in ("section", "kind", "external_id")},
        )
        entry = (query.select_for_update() if lock else query).first()
    unchanged = entry is not None and _snapshot(entry) == _serialized_values(values)
    _check_revision(arguments, entry, unchanged=unchanged)
    return entry, unchanged


def _dry_run(identity, organization, entry, values, reason, operation, unchanged):
    return ToolExecution(
        data={
            "status": "DRY_RUN", "operation": operation, "unchanged": unchanged,
            "entry_id": str(entry.id) if entry else None, "revision": entry.revision if entry else 0,
            "proposed_entry": _safe_snapshot(_serialized_values(values), body_limit=12000),
            "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_SETUP_INTAKE_WRITE),
            "reversible": True, "automatic_publication": False,
        },
        capability=CAP_SETUP_INTAKE_WRITE, target_type="setup_intake" if entry else "organization",
        target_id=str(entry.id) if entry else str(organization.id), reason=reason,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
        audit_summary={"operation": operation, "proposal_digest": _proposal_digest(_proposal(organization, entry, values))},
    )


def _save_entry(organization, actor, entry, values):
    if entry is None:
        return OperationsIntakeEntry.objects.create(organization=organization, created_by=actor, updated_by=actor, **values)
    previous = {"revision": entry.revision, "updated_at": entry.updated_at.isoformat(), "data": _snapshot(entry)}
    entry.history = [*entry.history[-(SETUP_INTAKE_HISTORY_LIMIT - 1):], previous]
    for field, value in values.items():
        setattr(entry, field, value)
    entry.revision += 1
    entry.updated_by = actor
    entry.save(update_fields=[*_DATA_FIELDS, "revision", "history", "updated_by", "updated_at"])
    return entry


def _write_result(entry, *, reason, operation, unchanged, expected_values):
    expected_revision = entry.revision
    entry.refresh_from_db()
    if entry.revision != expected_revision or _snapshot(entry) != _serialized_values(expected_values):
        raise OperationsToolError("Setup intake post-write verification failed.")
    return ToolExecution(
        data={"status": "UNCHANGED" if unchanged else "ARCHIVED" if operation == "archive_setup_intake_entry" else "FIXED",
              "entry": _serialize(entry, body_limit=12000), "verification": "passed", "automatic_publication": False},
        capability=CAP_SETUP_INTAKE_WRITE, target_type="setup_intake", target_id=str(entry.id), reason=reason,
        audit_summary={"operation": operation, "revision": entry.revision, "unchanged": unchanged, "verification": "passed"},
    )


def upsert_setup_intake_entry(*, identity, arguments):
    organization = _organization_for(identity)
    arguments = _arguments(arguments, _WRITE_FIELDS | {"entry_id", "data"})
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_SETUP_INTAKE_WRITE,
                                  tool_name="upsert_setup_intake_entry", arguments=arguments)
    values = _normalized_data(arguments.get("data"))
    entry, unchanged = _resolve_upsert(organization, arguments, values)
    if dry_run:
        return _dry_run(identity, organization, entry, values, reason, "upsert_setup_intake_entry", unchanged)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(organization, entry, values))
    with transaction.atomic():
        # The organization lock also serializes absent-row creation. Unique
        # constraints protect the stable source identity at the database boundary.
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        entry, unchanged = _resolve_upsert(organization, arguments, values, lock=True)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(organization, entry, values))
        if not unchanged:
            entry = _save_entry(organization, identity.actor, entry, values)
        return _write_result(entry, reason=reason, operation="upsert_setup_intake_entry", unchanged=unchanged, expected_values=values)


def archive_setup_intake_entry(*, identity, arguments):
    organization = _organization_for(identity)
    arguments = _arguments(arguments, _WRITE_FIELDS | {"entry_id"})
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_SETUP_INTAKE_WRITE,
                                  tool_name="archive_setup_intake_entry", arguments=arguments)
    _integer(arguments.get("expected_revision"), field="expected_revision", minimum=1)
    entry = _entry(organization, arguments.get("entry_id"))
    unchanged = not entry.is_active
    _check_revision(arguments, entry, unchanged=False)
    values = {**_snapshot(entry), "source_date": entry.source_date, "is_active": False}
    if dry_run:
        return _dry_run(identity, organization, entry, values, reason, "archive_setup_intake_entry", unchanged)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(organization, entry, values))
    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        entry = _entry(organization, arguments.get("entry_id"), lock=True)
        unchanged = not entry.is_active
        _check_revision(arguments, entry, unchanged=False)
        values = {**_snapshot(entry), "source_date": entry.source_date, "is_active": False}
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(organization, entry, values))
        if not unchanged:
            entry = _save_entry(organization, identity.actor, entry, values)
        return _write_result(entry, reason=reason, operation="archive_setup_intake_entry", unchanged=unchanged, expected_values=values)
