"""Operations MCP for the native, client-facing SHVYA Vault.

The canonical Vault service owns all persistence, client precedence, revisions,
private storage and quota checks. Lazy imports let capability discovery remain
available while an installation is deploying the Vault application.
"""

import base64
import binascii
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count

from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations.vault_catalog import CAP_VAULT_READ, CAP_VAULT_WRITE
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import ROLE_SUPERADMIN, approval_required
from apps.integrations.operations_tools import (
    OperationsPermissionError, OperationsToolError, ToolExecution,
    _ensure_approved_proposal_unchanged, _organization_for, _proposal_digest,
    _reject_secret_like_content, _require_operations_capability, _uuid, _write_gate,
)

MAX_UPLOAD_BYTES = 512 * 1024
MAX_TEXT_BYTES = 20000
_WRITE_FIELDS = {"reason", "dry_run", "approved", "approval_event_id"}
_ENTRY_FIELDS = {"section", "kind", "external_id", "body", "url", "origin", "source_date", "send_when", "allowed_for_ai_sharing"}
_QUESTION_FIELDS = {"section", "external_id", "text"}
_CALL_FIELDS = {"title", "date", "url", "summary", "attendees", "duration_min", "external_id", "share_recording"}
_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)
_VAULT_CREDENTIAL = re.compile(r"\bsv_[A-Za-z0-9_-]{12,}")


def _native():
    if not apps.is_installed("apps.vault"):
        raise OperationsToolError("Native SHVYA Vault is not installed in this deployment. Setup intake is a separate store.")
    from apps.vault import models, services
    from apps.vault.sections import SECTIONS
    return models, services, SECTIONS


def _args(arguments, allowed):
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise OperationsToolError("Unsupported Vault arguments.")
    return arguments


def _safe_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return "[REDACTED_URL]"
        # Client Vault slugs and signed-file paths are access material even when
        # no query string is present. Return no reusable client access links.
        if re.search(r"/(?:vault|vaults|vault-file|vault-files)(?:/|$)", parsed.path, re.I):
            return "[PRIVATE_VAULT_URL]"
        return sanitize_text(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), limit=2048)
    except ValueError:
        return "[REDACTED_URL]"


def _safe(value, limit=MAX_TEXT_BYTES):
    text = _URL.sub(lambda match: _safe_url(match.group()), str(value or ""))
    return sanitize_text(_VAULT_CREDENTIAL.sub("[REDACTED_VAULT_TOKEN]", text), limit=limit)


def _iso(value):
    return value.isoformat() if value else None


def _scope(identity, capability=CAP_VAULT_READ):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=capability)
    return organization


def _vault(organization, *, lock=False, required=True):
    models, _, _ = _native()
    rows = models.Vault.objects.filter(organization=organization)
    if lock:
        rows = rows.select_for_update()
    vault = rows.first()
    if vault is None and required:
        raise OperationsToolError("No native Vault exists for this organization. A Superadmin can create it.")
    return vault


def _entry(vault, entry_id):
    entry = vault.entries.filter(pk=_uuid(entry_id, field="entry_id")).first()
    if entry is None:
        raise OperationsToolError("Vault entry not found in this organization.")
    return entry


def _entry_payload(entry):
    return {
        "id": str(entry.pk), "section": entry.section, "kind": entry.kind,
        "body": _safe(entry.effective_body), "original_body": _safe(entry.body) if entry.client_body is not None else None,
        "client_body": _safe(entry.client_body) if entry.client_body is not None else None,
        "client_override_present": entry.client_body is not None,
        "url": _safe_url(entry.url), "author_type": entry.author_type, "origin": entry.origin,
        "source_label": _safe(entry.source_label, 250), "source_date": _iso(entry.source_date),
        "external_id": _safe(entry.external_id, 200), "confirmed_at": _iso(entry.confirmed_at),
        "client_edited_at": _iso(entry.client_edited_at), "file_name": _safe(entry.file_name, 200),
        "file_size": entry.file_size, "mime_type": entry.mime_type,
        "allowed_for_ai_sharing": entry.allowed_for_ai_sharing, "send_when": _safe(entry.send_when, 4000),
        "transcription_status": entry.transcription_status, "updated_at": _iso(entry.updated_at),
        "body_truncated": len(entry.effective_body) > MAX_TEXT_BYTES,
    }


def _question_payload(question):
    return {"id": str(question.pk), "section": question.section, "text": _safe(question.text, 2000),
            "answer": _safe(question.answer), "answered_at": _iso(question.answered_at),
            "external_id": _safe(question.external_id, 200), "author_type": question.author_type,
            "updated_at": _iso(question.updated_at)}


def _call_payload(call):
    return {"id": str(call.pk), "title": _safe(call.title, 200), "date": _iso(call.date),
            "url": _safe_url(call.url) if call.share_recording else "", "duration_min": call.duration_min,
            "attendees": [_safe(name, 200) for name in call.attendees], "summary": _safe(call.summary, 10000),
            "external_id": _safe(call.external_id, 200), "share_recording": call.share_recording,
            "recording_hidden": bool(call.url and not call.share_recording), "updated_at": _iso(call.updated_at)}


def _status(vault):
    _, _, definitions = _native()
    counts = dict(vault.entries.values("section").annotate(total=Count("pk")).values_list("section", "total"))
    rows = {row.key: row for row in vault.sections.all()}
    sections = []
    for definition in definitions:
        key = definition["key"]
        row = rows.get(key)
        sections.append({"key": key, "title": definition["title"], "state": row.state if row else "empty",
                         "is_done": bool(row and row.is_done), "entry_count": counts.get(key, 0),
                         "updated_at": _iso(row.updated_at) if row else None})
    return {"id": str(vault.pk), "organization_id": str(vault.organization_id), "name": _safe(vault.name, 255),
            "status": vault.status, "is_paused": vault.is_paused, "updated_at": _iso(vault.updated_at),
            "submitted_at": _iso(vault.submitted_at), "sections": sections, "section_count": len(sections),
            "entry_count": sum(counts.values()), "question_count": vault.questions.count(),
            "open_questions": vault.questions.filter(answered_at__isnull=True).count(), "call_count": vault.calls.count(),
            "storage_used_bytes": vault.storage_used_bytes, "storage_quota_bytes": vault.storage_quota_bytes,
            "completed_sections": sum(row["is_done"] or row["state"] == "dont_have" for row in sections),
            "content_classification": "client_vault_evidence", "automatic_publication": False}


def _read_result(vault, data, operation):
    return ToolExecution(data=data, capability=CAP_VAULT_READ, target_type="vault", target_id=str(vault.pk),
                         audit_summary={"operation": operation, "automatic_publication": False})


def get_vault(*, identity, arguments):
    _args(arguments, set())
    organization = _scope(identity)
    vault = _vault(organization, required=False)
    if vault is None:
        return ToolExecution(data={"exists": False, "organization_id": str(organization.pk), "automatic_publication": False},
                             capability=CAP_VAULT_READ, target_type="organization", target_id=str(organization.pk),
                             audit_summary={"operation": "get_vault", "exists": False})
    return _read_result(vault, {"exists": True, **_status(vault)}, "get_vault")


def export_vault(*, identity, arguments):
    _args(arguments, {"collection", "section", "limit", "cursor"})
    vault = _vault(_scope(identity))
    _, service, _ = _native()
    collection = arguments.get("collection", "entries")
    if collection not in {"entries", "questions", "calls"}:
        raise OperationsToolError("collection must be entries, questions or calls.")
    limit = arguments.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise OperationsToolError("limit must be between 1 and 50.")
    rows = getattr(vault, collection).all()
    if "section" in arguments:
        if collection == "calls":
            raise OperationsToolError("Calls are not associated with a section.")
        section = _validated(lambda: service._section(arguments["section"]))
        rows = rows.filter(section=section)
    if "cursor" in arguments:
        rows = rows.filter(pk__gt=_uuid(arguments["cursor"], field="cursor"))
    selected = list(rows.order_by("id")[:limit + 1])
    page = selected[:limit]
    serialize = {"entries": _entry_payload, "questions": _question_payload, "calls": _call_payload}[collection]
    return _read_result(vault, {
        "vault_id": str(vault.pk), "source_updated_at": _iso(vault.updated_at), "collection": collection,
        "items": [serialize(row) for row in page], "count": len(page),
        "next_cursor": str(page[-1].pk) if len(selected) > limit else None,
        "automatic_publication": False, "content_classification": "client_vault_evidence",
        "precedence": "Client edits override source text; client answers and confirmations are preserved.",
    }, "export_vault")


def get_vault_entry(*, identity, arguments):
    _args(arguments, {"entry_id"})
    vault = _vault(_scope(identity))
    return _read_result(vault, {"entry": _entry_payload(_entry(vault, arguments.get("entry_id")))}, "get_vault_entry")


def get_vault_asset(*, identity, arguments):
    _args(arguments, {"entry_id", "include_text"})
    vault = _vault(_scope(identity))
    entry = _entry(vault, arguments.get("entry_id"))
    if not entry.file or entry.kind not in {"file", "audio"}:
        raise OperationsToolError("This Vault entry has no private asset.")
    include_text = arguments.get("include_text", False)
    if not isinstance(include_text, bool):
        raise OperationsToolError("include_text must be a boolean.")
    result = {"entry": _entry_payload(entry), "text_available": False, "download": "authenticated_vault_ui"}
    if include_text:
        if entry.file_name.rsplit(".", 1)[-1].lower() in {"txt", "md", "csv"}:
            try:
                with entry.file.open("rb") as stream:
                    raw = stream.read(MAX_TEXT_BYTES + 1)
                result.update({"text_available": True, "text": _safe(raw[:MAX_TEXT_BYTES].decode("utf-8-sig", errors="replace")),
                               "text_truncated": len(raw) > MAX_TEXT_BYTES, "content_classification": "untrusted_uploaded_document"})
            except (OSError, ValidationError, ValueError):
                raise OperationsToolError("The private asset is unavailable. Inspect its storage through Superadmin.") from None
        else:
            result["preview_limitation"] = "Only TXT, MD and CSV have inline text previews. Use the authenticated Vault UI for binary assets."
    return _read_result(vault, result, "get_vault_asset")


def _validated(callback):
    try:
        return callback()
    except ValidationError as exc:
        raise OperationsToolError(_safe(" ".join(exc.messages), 1000)) from exc


def _input(data, allowed):
    if not isinstance(data, dict) or set(data) - allowed:
        raise OperationsToolError("Vault data contains unsupported or protected fields.")
    _reject_secret_like_content(data, field="vault")
    # Canonical service rejects credentials in URLs; additionally prevent storing
    # signed/token-bearing links or client access URLs through an external agent.
    for value in data.values():
        if isinstance(value, str):
            if _VAULT_CREDENTIAL.search(value):
                raise OperationsToolError("Vault tokens cannot be stored in customer-visible evidence.")
            for match in _URL.finditer(value):
                parsed = urlsplit(match.group())
                if parsed.query or parsed.fragment or parsed.username or parsed.password or _safe_url(match.group()) != match.group():
                    raise OperationsToolError("Use public stable source links without credentials, signatures or private Vault access URLs.")
    return dict(data)


def _normalize(service, data, kind):
    allowed = {"entry": _ENTRY_FIELDS, "question": _QUESTION_FIELDS, "call": _CALL_FIELDS}[kind]
    data = _input(data, allowed)
    external_id = service._external_id(data.get("external_id"))
    if not external_id:
        raise OperationsToolError("A stable external_id is required to make Vault retries idempotent.")
    data["external_id"] = external_id
    if kind == "entry":
        data["section"] = service._section(data.get("section"))
        data["kind"] = data.get("kind", "note")
        if data["kind"] not in {"note", "link", "file", "audio"}:
            raise OperationsToolError("Unsupported Vault entry kind.")
        data["body"] = service._text(data.get("body", ""), "body", required=data["kind"] == "note")
        if len(data["body"].split()) > 300:
            raise OperationsToolError("Keep each agent Vault note to 300 words or fewer.")
        data["url"] = service._url(data.get("url", ""))
        if data["kind"] == "link" and not data["url"]:
            raise OperationsToolError("A link entry requires url.")
        data["origin"] = data.get("origin", "other")
        if data["origin"] not in {"fireflies", "whatsapp", "ops_chat", "other"}:
            raise OperationsToolError("Unsupported Vault source origin.")
        data["source_date"] = _iso(service._date(data.get("source_date"), "source_date"))
        for field in ("send_when", "allowed_for_ai_sharing"):
            if field in data:
                data[field] = service._text(data[field], field, 4000) if field == "send_when" else service._boolean(data[field], field)
    elif kind == "question":
        data["section"] = service._section(data.get("section", "other"))
        data["text"] = service._text(data.get("text", ""), "text", 2000, True)
    else:
        data["title"] = service._text(data.get("title", ""), "title", 200, True)
        data["date"] = _iso(service._date(data.get("date"), "date", True))
        data["url"] = service._url(data.get("url", ""))
        data["summary"] = service._text(data.get("summary", ""), "summary", 10000)
        attendees = data.get("attendees", [])
        if not isinstance(attendees, list) or len(attendees) > 100:
            raise OperationsToolError("attendees must contain at most 100 names.")
        data["attendees"] = [service._text(name, "attendees", 200, True) for name in attendees]
        duration = data.get("duration_min")
        if duration is not None and (type(duration) is not int or not 0 <= duration <= 10080):
            raise OperationsToolError("duration_min must be between 0 and 10080 or null.")
        data["duration_min"] = duration
        if "share_recording" in data:
            data["share_recording"] = service._boolean(data["share_recording"], "share_recording")
    return data


def _existing(vault, data, kind):
    collection = {"entry": "entries", "question": "questions", "call": "calls"}[kind]
    rows = getattr(vault, collection).filter(external_id=data["external_id"])
    if kind != "call":
        rows = rows.filter(author_type="agent")
    row = rows.first()
    if row and kind == "entry" and (row.section != data["section"] or row.kind != data["kind"]):
        raise OperationsToolError("An external ID cannot change an entry's section or kind.")
    if row and kind == "question" and row.answered_at and (row.text != data["text"] or row.section != data["section"]):
        raise OperationsToolError("This question has a client answer. Create a new question instead.")
    return row


def _before(row):
    if row is None:
        return None
    # Raw state is hashed for approval binding, never included in audit metadata.
    result = {}
    for field in row._meta.concrete_fields:
        value = getattr(row, field.attname)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif field.get_internal_type() in {"UUIDField", "FileField"}:
            value = str(value)
        result[field.attname] = value
    return result


def _proposal(vault, row, data, *, upload=None):
    result = {"vault_id": str(vault.pk), "organization_id": str(vault.organization_id),
              "vault_updated_at": _iso(vault.updated_at), "before": _before(row), "after": data}
    if upload:
        result["asset"] = upload
        result["storage_used_bytes"] = vault.storage_used_bytes
        result["storage_quota_bytes"] = vault.storage_quota_bytes
    return result


def _preview(identity, organization, vault, operation, proposal, reason, details):
    return ToolExecution(data={"status": "DRY_RUN", "operation": operation, **details,
                              "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_VAULT_WRITE),
                              "client_visible": True, "automatic_publication": False},
                         capability=CAP_VAULT_WRITE, target_type="vault" if vault else "organization",
                         target_id=str(vault.pk if vault else organization.pk), reason=reason,
                         outcome=OperationsAuditEvent.Outcome.DRY_RUN,
                         audit_summary={"operation": operation, "proposal_digest": _proposal_digest(proposal), "client_visible": True})


def create_vault_workspace(*, identity, arguments):
    _args(arguments, _WRITE_FIELDS | {"name"})
    organization = _organization_for(identity)
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError("Creating a native client Vault requires SHVYA Superadmin.")
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_VAULT_WRITE,
                                  tool_name="create_vault_workspace", arguments=arguments)
    _, service, _ = _native()
    name = _validated(lambda: service._text(arguments.get("name", organization.name), "name", 255, True))
    _input({"name": name}, {"name"})
    existing = _vault(organization, required=False)
    proposal = {"organization_id": str(organization.pk), "name": name, "existing_vault_id": str(existing.pk) if existing else None}
    if dry_run:
        return _preview(identity, organization, existing, "create_vault_workspace", proposal, reason,
                        {"name": name, "already_exists": existing is not None, "credentials_delivery": "authenticated_superadmin_ui"})
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        existing = _vault(organization, lock=True, required=False)
        proposal["existing_vault_id"] = str(existing.pk) if existing else None
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        if existing is None:
            vault, _access_code = _validated(lambda: service.create_vault(organization, name=name))
        else:
            vault = existing
    return ToolExecution(data={"status": "UNCHANGED" if existing else "CREATED", "vault": _status(vault),
                               "credentials_delivery": "Rotate/view access credentials only in authenticated Superadmin Vault controls.",
                               "verification": "passed"}, capability=CAP_VAULT_WRITE, target_type="vault", target_id=str(vault.pk), reason=reason,
                         audit_summary={"operation": "create_vault_workspace", "verification": "passed"})


def _upsert(identity, arguments, kind, operation, *, asset=False):
    _args(arguments, _WRITE_FIELDS | {"data"})
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_VAULT_WRITE,
                                  tool_name=operation, arguments=arguments)
    vault = _vault(organization)
    _, service, _ = _native()
    raw_data = arguments.get("data")
    upload = descriptor = None
    if asset:
        if not isinstance(raw_data, dict) or set(raw_data) - (_ENTRY_FIELDS | {"filename", "content_base64"}):
            raise OperationsToolError("Asset data contains unsupported fields.")
        raw_data = dict(raw_data)
        filename = raw_data.pop("filename", None)
        encoded = raw_data.pop("content_base64", None)
        if not isinstance(filename, str) or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
            raise OperationsToolError("Supply filename and valid base64 for an asset of at most 512 KiB.")
        _input({"filename": filename}, {"filename"})
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise OperationsToolError("content_base64 is invalid.") from None
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise OperationsToolError("A Vault MCP upload must contain 1 byte to 512 KiB.")
        raw_data.setdefault("kind", "file")
        if raw_data["kind"] not in {"file", "audio"}:
            raise OperationsToolError("Asset kind must be file or audio.")
        upload = ContentFile(raw, name=filename)
        name, size, mime = _validated(lambda: service.validate_upload(upload, raw_data["kind"]))
        descriptor = {"filename": name, "size": size, "mime_type": mime, "sha256": hashlib.sha256(raw).hexdigest()}
    data = _validated(lambda: _normalize(service, raw_data, kind))
    row = _existing(vault, data, kind)
    if kind == "entry":
        data.setdefault("send_when", row.send_when if row else "")
        data.setdefault("allowed_for_ai_sharing", row.allowed_for_ai_sharing if row else False)
        if data["kind"] in {"file", "audio"} and not upload and (not row or not row.file):
            raise OperationsToolError("Use upload_vault_asset to create file and audio entries.")
    elif kind == "call":
        data.setdefault("share_recording", row.share_recording if row else False)
    if descriptor and vault.storage_used_bytes - (row.file_size if row else 0) + descriptor["size"] > vault.storage_quota_bytes:
        raise OperationsToolError("This Vault has reached its storage quota.")
    proposal = _proposal(vault, row, data, upload=descriptor)
    if dry_run:
        return _preview(identity, organization, vault, operation, proposal, reason,
                        {"item_id": str(row.pk) if row else None, "proposed": data, "asset": descriptor,
                         "client_override_preserved": bool(kind == "entry" and row and row.client_body is not None)})
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    saved = None
    try:
        with transaction.atomic():
            locked = _vault(organization, lock=True)
            row = _existing(locked, data, kind)
            _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(locked, row, data, upload=descriptor))
            if kind == "entry":
                saved, updated = _validated(lambda: service.upsert_entry(locked, data, author_type="agent", actor=identity.actor, upload=upload))
                serializer = _entry_payload
            elif kind == "question":
                saved, updated = _validated(lambda: service.upsert_question(locked, data, author_type="agent"))
                serializer = _question_payload
            else:
                saved, updated = _validated(lambda: service.upsert_call(locked, data))
                serializer = _call_payload
            saved.refresh_from_db()
            if saved.vault_id != locked.pk or saved.external_id != data["external_id"]:
                raise OperationsToolError("Vault post-write verification failed.")
            for key, expected in data.items():
                actual = getattr(saved, key)
                if hasattr(actual, "isoformat"):
                    actual = actual.isoformat()
                if actual != expected:
                    raise OperationsToolError("Vault post-write content verification failed.")
            if descriptor and (saved.file_size != descriptor["size"] or saved.file_name != descriptor["filename"]):
                raise OperationsToolError("Vault asset verification failed.")
            if descriptor:
                with saved.file.open("rb") as stored:
                    stored_digest = hashlib.sha256(stored.read(MAX_UPLOAD_BYTES + 1)).hexdigest()
                if stored_digest != descriptor["sha256"]:
                    raise OperationsToolError("Vault asset content verification failed.")
            payload = serializer(saved)
    except Exception:
        # Canonical storage cleans failures inside upsert_entry. Also clean a
        # newly written file if verification fails in this outer transaction.
        if descriptor and saved is not None and saved.file:
            saved.file.storage.delete(saved.file.name)
        raise
    return ToolExecution(data={"status": "UPDATED" if updated else "CREATED", "item": payload,
                               "verification": "passed", "client_visible": True, "automatic_publication": False},
                         capability=CAP_VAULT_WRITE, target_type="vault_" + kind, target_id=str(saved.pk), reason=reason,
                         audit_summary={"operation": operation, "updated": updated, "verification": "passed", "client_visible": True})


def upsert_vault_entry(*, identity, arguments):
    return _upsert(identity, arguments, "entry", "upsert_vault_entry")


def upsert_vault_question(*, identity, arguments):
    return _upsert(identity, arguments, "question", "upsert_vault_question")


def upsert_vault_call(*, identity, arguments):
    return _upsert(identity, arguments, "call", "upsert_vault_call")


def upload_vault_asset(*, identity, arguments):
    return _upsert(identity, arguments, "entry", "upload_vault_asset", asset=True)


def set_vault_section(*, identity, arguments):
    _args(arguments, _WRITE_FIELDS | {"section", "state", "is_done"})
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_VAULT_WRITE,
                                  tool_name="set_vault_section", arguments=arguments)
    vault = _vault(organization)
    _, service, _ = _native()
    key = _validated(lambda: service._section(arguments.get("section")))
    data = {field: arguments[field] for field in ("state", "is_done") if field in arguments}
    if not data or ("state" in data and data["state"] not in {"empty", "filled", "dont_have"}) or ("is_done" in data and type(data["is_done"]) is not bool):
        raise OperationsToolError("Provide a valid section state or boolean is_done.")
    row = vault.sections.filter(key=key).first()
    has_content = vault.entries.filter(section=key).exists() or vault.questions.filter(section=key, answered_at__isnull=False).exists()
    state = data.get("state", row.state if row else "empty")
    if (state in {"empty", "dont_have"} and has_content) or (state == "filled" and not has_content) or (data.get("is_done", bool(row and row.is_done)) and state == "empty"):
        raise OperationsToolError("Section progress must agree with its actual content.")
    proposal = _proposal(vault, row, {"section": key, **data})
    if dry_run:
        return _preview(identity, organization, vault, "set_vault_section", proposal, reason, {"proposed": {"section": key, **data}})
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        locked = _vault(organization, lock=True)
        row = locked.sections.filter(key=key).first()
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=_proposal(locked, row, {"section": key, **data}))
        saved = _validated(lambda: service.set_section_state(locked, key, actor_type="agent", **data))
        saved.refresh_from_db()
        if any(getattr(saved, field) != expected for field, expected in data.items()):
            raise OperationsToolError("Vault section verification failed.")
    return ToolExecution(data={"status": "UPDATED", "section": {"key": key, "state": saved.state, "is_done": saved.is_done},
                               "client_visible": True, "automatic_publication": False, "verification": "passed"},
                         capability=CAP_VAULT_WRITE, target_type="vault", target_id=str(vault.pk), reason=reason,
                         audit_summary={"operation": "set_vault_section", "section": key, "verification": "passed"})


VAULT_TOOL_HANDLERS = {name: globals()[name] for name in (
    "get_vault", "export_vault", "get_vault_entry", "get_vault_asset", "create_vault_workspace",
    "upsert_vault_entry", "upsert_vault_question", "upsert_vault_call", "upload_vault_asset", "set_vault_section",
)}
