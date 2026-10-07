"""Tenant-scoped Vault operations. No operation writes production AI configuration."""
import hashlib
import mimetypes
import ntpath
import re
import secrets
from datetime import date, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (Vault, VaultCall, VaultEntry, VaultEntryRevision, VaultEvent,
                     VaultProfileSnapshot, VaultQuestion, VaultSection)
from .sections import ORIGIN_CHOICES, SECTIONS, SECTION_KEYS
from .storage import MAX_PLAINTEXT

MAX_BODY = 20000
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".md", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mp3", ".m4a", ".wav", ".ogg"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".webm", ".mp4"}


class VaultConflict(ValidationError):
    """The client attempted to overwrite an entry changed since it was displayed."""


def _text(value, field, limit=MAX_BODY, required=False):
    if not isinstance(value, str):
        raise ValidationError({field: "Must be text."})
    value = value.strip()
    if required and not value:
        raise ValidationError({field: "This field is required."})
    if len(value) > limit:
        raise ValidationError({field: f"Use no more than {limit} characters."})
    if "\x00" in value:
        raise ValidationError({field: "Contains an invalid character."})
    return value


def _boolean(value, field):
    if not isinstance(value, bool):
        raise ValidationError({field: "Must be true or false."})
    return value


def _section(value):
    if not isinstance(value, str) or value not in SECTION_KEYS:
        raise ValidationError({"section": "Choose a valid Vault section."})
    return value


def _external_id(value):
    return _text("" if value is None else value, "external_id", 200) or None


def _date(value, field, required=False):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "This field is required."})
        return None
    if type(value) is date:
        return value
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError({field: "Use a valid date in YYYY-MM-DD format."})


def _url(value):
    value = _text(value or "", "url", 2048)
    if value:
        URLValidator(schemes=["http", "https"])(value)
        if urlsplit(value).username or urlsplit(value).password:
            raise ValidationError({"url": "Do not include credentials in links."})
    return value


def _actor(actor):
    return actor if getattr(actor, "is_authenticated", False) else None


def _touch(vault, actor_type=None, section=""):
    now = timezone.now()
    Vault.objects.filter(pk=vault.pk).update(updated_at=now)
    vault.updated_at = now
    if actor_type == "client":
        VaultEvent.objects.create(vault=vault, kind="updated", section=section, actor_type="client")


def _fill_section(vault, section):
    obj, _ = VaultSection.objects.get_or_create(vault=vault, key=section)
    obj.state = VaultSection.State.FILLED
    obj.save(update_fields=["state", "updated_at"])


@transaction.atomic
def create_vault(organization, name=None):
    code = f"{secrets.randbelow(1000000):06d}"
    vault = Vault.objects.create(organization=organization, name=_text(name or organization.name, "name", 255, True), access_code_hash=make_password(code))
    VaultSection.objects.bulk_create([VaultSection(vault=vault, key=key) for key in SECTION_KEYS])
    VaultEvent.objects.create(vault=vault, kind="created", actor_type="team")
    return vault, code


@transaction.atomic
def rotate_access_code(vault):
    current = Vault.objects.select_for_update().get(pk=vault.pk)
    code = f"{secrets.randbelow(1000000):06d}"
    while current.check_access_code(code):
        code = f"{secrets.randbelow(1000000):06d}"
    current.access_code_hash = make_password(code)
    current.access_version += 1
    current.failed_access_attempts = 0
    current.access_locked_until = None
    current.save(update_fields=["access_code_hash", "access_version", "failed_access_attempts", "access_locked_until", "updated_at"])
    VaultEvent.objects.create(vault=current, kind="access_rotated", actor_type="team")
    vault.refresh_from_db()
    return code


@transaction.atomic
def rotate_agent_token(vault):
    current = Vault.objects.select_for_update().get(pk=vault.pk)
    token = "sv_" + secrets.token_urlsafe(36)
    current.token_hash = hashlib.sha256(token.encode()).hexdigest()
    current.token_prefix = token[:11]
    current.token_created_at = timezone.now()
    current.token_expires_at = current.token_created_at + timedelta(days=90)
    current.save(update_fields=["token_hash", "token_prefix", "token_created_at", "token_expires_at", "updated_at"])
    VaultEvent.objects.create(vault=current, kind="token_rotated", actor_type="team")
    vault.refresh_from_db()
    return token


def authenticate_agent_token(token):
    if not isinstance(token, str) or not re.fullmatch(r"sv_[A-Za-z0-9_-]{40,100}", token):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return Vault.objects.filter(token_hash=digest, token_expires_at__gt=timezone.now()).first()


def validate_storage_quota(value, used_bytes=0):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not value.isdigit()):
        raise ValidationError({"storage_quota_bytes": "Enter a valid storage quota."})
    try:
        quota = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError({"storage_quota_bytes": "Enter a valid storage quota."})
    if quota < int(used_bytes) or quota < 1024 * 1024 or quota > 1024 ** 4:
        raise ValidationError({"storage_quota_bytes": "Quota must cover existing files and be between 1 MiB and 1 TiB."})
    return quota


def validate_upload(upload, kind="file"):
    name = ntpath.basename(str(upload.name or "file"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")[:200]
    if not name or "." not in name:
        raise ValidationError({"file": "Upload a file with a supported extension."})
    extension = "." + name.rsplit(".", 1)[-1].lower()
    if extension not in ALLOWED_EXTENSIONS or (kind == "audio" and extension not in AUDIO_EXTENSIONS):
        raise ValidationError({"file": "This file type is not supported."})
    max_size = min(MAX_PLAINTEXT, int(getattr(settings, "VAULT_MAX_FILE_BYTES", 25 * 1024 * 1024)))
    if not upload.size or upload.size > max_size:
        raise ValidationError({"file": f"Upload a non-empty file no larger than {max_size // (1024 * 1024)} MB."})
    # File type is derived from the safe extension, never trusted from the browser.
    # All downloads are attachment-only with nosniff; no uploaded content is executed.
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    checks = {".pdf": head.startswith(b"%PDF-"), ".png": head.startswith(b"\x89PNG\r\n\x1a\n"), ".jpg": head.startswith(b"\xff\xd8\xff"), ".jpeg": head.startswith(b"\xff\xd8\xff"), ".gif": head.startswith((b"GIF87a", b"GIF89a")), ".docx": head.startswith(b"PK"), ".xlsx": head.startswith(b"PK"), ".pptx": head.startswith(b"PK")}
    if extension in checks and not checks[extension]:
        raise ValidationError({"file": "The file content does not match its extension."})
    return name, upload.size, mime


def _revision(entry, actor_type, actor=None):
    VaultEntryRevision.objects.create(entry=entry, body=entry.body, client_body=entry.client_body, actor_type=actor_type, actor=_actor(actor), metadata={"url": entry.url, "section": entry.section, "confirmed_at": entry.confirmed_at.isoformat() if entry.confirmed_at else None, "allowed_for_ai_sharing": entry.allowed_for_ai_sharing, "send_when": entry.send_when})


def upsert_entry(vault, data, author_type="agent", actor=None, upload=None):
    if not isinstance(data, dict):
        raise ValidationError("Entry must be an object.")
    if author_type not in VaultEntry.Author.values:
        raise ValidationError("Invalid author type.")
    section = _section(data.get("section"))
    kind = data.get("kind", "file" if upload else "note")
    if kind not in VaultEntry.Kind.values:
        raise ValidationError({"kind": "Choose a valid entry kind."})
    body = _text(data.get("body", ""), "body", required=kind == "note")
    if author_type == "agent" and len(body.split()) > 300:
        raise ValidationError({"body": "Keep each agent note to 300 words or fewer."})
    url = _url(data.get("url", ""))
    if kind == "link" and not url:
        raise ValidationError({"url": "A link is required."})
    if upload and kind not in {"file", "audio"}:
        raise ValidationError({"kind": "Choose file or audio for uploads."})
    origin = data.get("origin", "other")
    if not isinstance(origin, str) or origin not in dict(ORIGIN_CHOICES):
        raise ValidationError({"origin": "Choose a valid source."})
    source_date = _date(data.get("source_date"), "source_date")
    external_id = _external_id(data.get("external_id"))
    send_when = _text(data.get("send_when", ""), "send_when", 4000)
    share = _boolean(data.get("allowed_for_ai_sharing", False), "allowed_for_ai_sharing")
    file_meta = validate_upload(upload, kind) if upload else None
    saved_file = None
    try:
        with transaction.atomic():
            locked = Vault.objects.select_for_update().get(pk=vault.pk)
            entry = VaultEntry.objects.filter(vault=locked, author_type=author_type, external_id=external_id).first() if external_id else None
            updated = entry is not None
            if kind in {"file", "audio"} and upload is None and (entry is None or not entry.file):
                raise ValidationError({"file": "A file is required."})
            if entry:
                if entry.kind != kind or entry.section != section:
                    raise VaultConflict("An external ID cannot move an existing entry or change its type.")
                if "send_when" not in data:
                    send_when = entry.send_when
                if "allowed_for_ai_sharing" not in data:
                    share = entry.allowed_for_ai_sharing
                source_changed = (entry.body != body or entry.url != url or entry.origin != origin or entry.source_date != source_date or bool(upload) or entry.send_when != send_when or entry.allowed_for_ai_sharing != share)
                if source_changed:
                    _revision(entry, author_type, actor)
            else:
                entry = VaultEntry(vault=locked, author_type=author_type, section=section, kind=kind, external_id=external_id, created_by=_actor(actor))
            entry.body, entry.url, entry.origin, entry.source_date = body, url, origin, source_date
            entry.send_when, entry.allowed_for_ai_sharing = send_when, share
            # A changed source needs fresh confirmation; a client override is retained.
            if updated and source_changed and entry.client_body is None and author_type != "client":
                entry.confirmed_at = None
            if file_meta:
                name, size, mime = file_meta
                new_total = locked.storage_used_bytes - entry.file_size + size
                if new_total > locked.storage_quota_bytes:
                    raise ValidationError({"file": "This Vault has reached its storage quota."})
                old_name = entry.file.name
                entry.file.save(name, upload, save=False)
                saved_file = entry.file.name
                entry.file_name, entry.file_size, entry.mime_type = name, size, mime
                locked.storage_used_bytes = new_total
                locked.save(update_fields=["storage_used_bytes", "updated_at"])
                if old_name:
                    transaction.on_commit(lambda: entry.file.storage.delete(old_name))
            if kind == "audio":
                entry.transcription_status = "provided" if body else "unavailable"
            if author_type == "client":
                entry.confirmed_at = timezone.now()
            entry.save()
            _fill_section(locked, section)
            _touch(locked, author_type, section)
        vault.refresh_from_db()
        return entry, updated
    except Exception:
        if saved_file:
            entry.file.storage.delete(saved_file)
        raise


@transaction.atomic
def edit_client_entry(vault, entry_id, body, expected_updated_at=None, actor=None):
    Vault.objects.select_for_update().get(pk=vault.pk)
    entry = VaultEntry.objects.select_for_update().get(vault=vault, pk=entry_id)
    if expected_updated_at:
        expected = parse_datetime(expected_updated_at) if isinstance(expected_updated_at, str) else expected_updated_at
        if expected is None or expected != entry.updated_at:
            raise VaultConflict("This entry changed. Reload it before saving your edit.")
    body = _text(body, "body", required=entry.kind == "note")
    _revision(entry, "client", actor)
    entry.client_body = body
    entry.client_edited_at = timezone.now()
    entry.confirmed_at = timezone.now()
    if entry.kind == "audio":
        entry.transcription_status = "provided" if body else "unavailable"
    entry.save(update_fields=["client_body", "client_edited_at", "confirmed_at", "transcription_status", "updated_at"])
    _touch(vault, "client", entry.section)
    return entry


@transaction.atomic
def confirm_entry(vault, entry_id):
    Vault.objects.select_for_update().get(pk=vault.pk)
    entry = VaultEntry.objects.get(vault=vault, pk=entry_id)
    entry.confirmed_at = timezone.now()
    entry.save(update_fields=["confirmed_at", "updated_at"])
    _touch(vault, "client", entry.section)
    return entry


@transaction.atomic
def delete_entry(vault, entry_id, actor_type="client", actor=None):
    locked = Vault.objects.select_for_update().get(pk=vault.pk)
    entry = VaultEntry.objects.get(vault=locked, pk=entry_id)
    section = entry.section
    locked.storage_used_bytes = max(0, locked.storage_used_bytes - entry.file_size)
    locked.save(update_fields=["storage_used_bytes", "updated_at"])
    entry.delete()
    if not locked.entries.filter(section=section).exists() and not locked.questions.filter(section=section, answered_at__isnull=False).exists():
        VaultSection.objects.filter(vault=locked, key=section).update(state="empty", is_done=False)
    _touch(locked, actor_type, section)
    vault.refresh_from_db()


@transaction.atomic
def set_section_state(vault, key, state=None, is_done=None, actor_type="client"):
    key = _section(key)
    Vault.objects.select_for_update().get(pk=vault.pk)
    section, _ = VaultSection.objects.get_or_create(vault=vault, key=key)
    if state is not None:
        if state not in VaultSection.State.values:
            raise ValidationError({"state": "Choose a valid section state."})
        has_content = vault.entries.filter(section=key).exists() or vault.questions.filter(section=key, answered_at__isnull=False).exists()
        if state in {"empty", "dont_have"} and has_content:
            raise ValidationError({"state": "This section contains material. Remove it before marking the section empty or unavailable."})
        if state == "filled" and not has_content:
            raise ValidationError({"state": "Add content before marking this section filled."})
        section.state = state
    if is_done is not None:
        section.is_done = _boolean(is_done, "is_done")
        if is_done and section.state == "empty":
            raise ValidationError({"state": "Add content or choose 'We don't have this' first."})
    section.save()
    _touch(vault, actor_type, key)
    return section


@transaction.atomic
def upsert_question(vault, data, author_type="agent"):
    section = _section(data.get("section", "other"))
    text = _text(data.get("text", ""), "text", 2000, True)
    external_id = _external_id(data.get("external_id"))
    Vault.objects.select_for_update().get(pk=vault.pk)
    question = VaultQuestion.objects.filter(vault=vault, author_type=author_type, external_id=external_id).first() if external_id else None
    updated = question is not None
    if question:
        if question.answered_at and (question.text != text or question.section != section):
            raise VaultConflict("This question already has a client answer. Add a new question instead.")
        question.text, question.section = text, section
        question.save()
    else:
        question = VaultQuestion.objects.create(vault=vault, section=section, text=text, external_id=external_id, author_type=author_type)
    _touch(vault)
    return question, updated


@transaction.atomic
def answer_question(vault, question_id, answer):
    Vault.objects.select_for_update().get(pk=vault.pk)
    question = VaultQuestion.objects.get(vault=vault, pk=question_id)
    question.answer = _text(answer, "answer", required=True)
    question.answered_at = timezone.now()
    question.save(update_fields=["answer", "answered_at", "updated_at"])
    _fill_section(vault, question.section)
    _touch(vault, "client", question.section)
    return question


@transaction.atomic
def upsert_call(vault, data):
    title = _text(data.get("title", ""), "title", 200, True)
    call_date = _date(data.get("date"), "date", True)
    url = _url(data.get("url", ""))
    summary = _text(data.get("summary", ""), "summary", 10000)
    attendees = data.get("attendees", [])
    if not isinstance(attendees, list) or len(attendees) > 100:
        raise ValidationError({"attendees": "Provide a list of up to 100 names."})
    attendees = [_text(name, "attendees", 200, True) for name in attendees]
    duration = data.get("duration_min")
    if duration is not None and (type(duration) is not int or duration < 0 or duration > 10080):
        raise ValidationError({"duration_min": "Provide a duration between 0 and 10080 minutes."})
    share = _boolean(data.get("share_recording", True), "share_recording")
    external_id = _external_id(data.get("external_id"))
    Vault.objects.select_for_update().get(pk=vault.pk)
    call = VaultCall.objects.filter(vault=vault, external_id=external_id).first() if external_id else None
    updated = call is not None
    if call and "share_recording" not in data:
        share = call.share_recording
    call = call or VaultCall(vault=vault, external_id=external_id)
    call.title, call.date, call.url, call.summary = title, call_date, url, summary
    call.attendees, call.duration_min, call.share_recording = attendees, duration, share
    call.save()
    _touch(vault)
    return call, updated


@transaction.atomic
def submit_vault(vault):
    current = Vault.objects.select_for_update().get(pk=vault.pk)
    if not current.sections.filter(state__in=["filled", "dont_have"]).exists():
        raise ValidationError("Add material or mark an unavailable section before submitting.")
    current.status = Vault.Status.SUBMITTED
    current.submitted_at = timezone.now()
    current.save(update_fields=["status", "submitted_at", "updated_at"])
    VaultEvent.objects.create(vault=current, kind="submitted", actor_type="client")
    vault.refresh_from_db()
    return vault


def signed_download_url(entry, request=None, purpose="client"):
    if not entry.file:
        return ""
    if purpose not in {"client", "agent", "staff"}:
        raise ValueError("Invalid download purpose.")
    vault = entry.vault
    payload = {"entry": str(entry.pk), "vault": str(vault.pk), "version": vault.access_version, "purpose": purpose}
    if purpose in {"agent", "staff"}:
        payload["token"] = (vault.token_hash or "")[:24]
    signature = signing.dumps(payload, salt="shvya-vault-download", compress=True)
    path = reverse("vault-signed-file", kwargs={"entry_id": entry.pk}) + "?signature=" + signature
    return request.build_absolute_uri(path) if request else path


def verify_download_signature(entry, signature):
    try:
        payload = signing.loads(signature, salt="shvya-vault-download", max_age=3600)
    except (signing.BadSignature, TypeError, ValueError):
        return False
    vault = entry.vault
    if payload.get("entry") != str(entry.pk) or payload.get("vault") != str(vault.pk) or payload.get("version") != vault.access_version:
        return False
    purpose = payload.get("purpose")
    if purpose == "client":
        return not vault.is_paused
    if purpose == "agent":
        return bool(vault.token_hash and vault.token_expires_at and vault.token_expires_at > timezone.now() and secrets.compare_digest(payload.get("token", ""), vault.token_hash[:24]))
    # Staff downloads require a staff session, never a free-standing signed URL.
    return False


def _iso(value):
    return value.isoformat() if value else None


def entry_payload(entry, request=None, purpose="client"):
    download_url = signed_download_url(entry, request, purpose) if entry.file else ""
    return {"id": str(entry.pk), "section": entry.section, "kind": entry.kind, "body": entry.effective_body, "original_body": entry.body if entry.client_body is not None else None, "client_body": entry.client_body, "url": entry.url, "file_name": entry.file_name, "file_size": entry.file_size, "size": entry.file_size, "mime_type": entry.mime_type, "download_url": download_url if entry.kind != "audio" else "", "audio_url": download_url if entry.kind == "audio" else "", "author_type": entry.author_type, "source_label": entry.source_label, "origin": entry.origin, "source_date": _iso(entry.source_date), "external_id": entry.external_id, "confirmed_at": _iso(entry.confirmed_at), "client_edited_at": _iso(entry.client_edited_at), "allowed_for_ai_sharing": entry.allowed_for_ai_sharing, "send_when": entry.send_when, "transcription_status": entry.transcription_status, "created_at": _iso(entry.created_at), "updated_at": _iso(entry.updated_at)}


def question_payload(question):
    return {"id": str(question.pk), "section": question.section, "text": question.text, "answer": question.answer, "answered_at": _iso(question.answered_at), "external_id": question.external_id, "created_at": _iso(question.created_at), "updated_at": _iso(question.updated_at)}


def call_payload(call, include_private_recording=False):
    return {"id": str(call.pk), "title": call.title, "date": _iso(call.date), "url": call.url if call.share_recording or include_private_recording else "", "duration_min": call.duration_min, "attendees": call.attendees, "summary": call.summary, "external_id": call.external_id, "share_recording": call.share_recording}


def workspace_payload(vault, request=None, purpose="client"):
    # Explicit allow-list deliberately excludes hashes, credentials and internal organization data.
    entries = [entry_payload(entry, request, purpose) for entry in vault.entries.select_related("vault")]
    questions = [question_payload(q) for q in vault.questions.all()]
    section_rows = {s.key: s for s in vault.sections.all()}
    sections = []
    for definition in SECTIONS:
        row = section_rows.get(definition["key"])
        section_entries = [e for e in entries if e["section"] == definition["key"]]
        sections.append({**definition, "state": row.state if row else "empty", "is_done": bool(row and row.is_done), "entry_count": len(section_entries), "entries": section_entries, "questions": [q for q in questions if q["section"] == definition["key"]]})
    completed = sum(s["is_done"] or s["state"] == "dont_have" for s in sections)
    return {"id": str(vault.pk), "name": vault.name, "slug": vault.slug, "status": vault.status, "is_paused": vault.is_paused, "submitted_at": _iso(vault.submitted_at), "created_at": _iso(vault.created_at), "updated_at": _iso(vault.updated_at), "storage_used_bytes": vault.storage_used_bytes, "storage_quota_bytes": vault.storage_quota_bytes, "completed_sections": completed, "filled_sections": sum(s["state"] == "filled" for s in sections), "resolved_sections": sum(s["state"] != "empty" for s in sections), "section_count": len(SECTIONS), "progress_percent": round(completed / len(SECTIONS) * 100), "open_questions": sum(q["answered_at"] is None for q in questions), "entry_count": len(entries), "file_count": sum(bool(e["file_name"]) for e in entries), "sections": sections, "entries": entries, "questions": questions, "calls": [call_payload(c) for c in vault.calls.all()]}


def export_markdown(vault):
    workspace = workspace_payload(vault)
    lines = [f"# SHVYA Vault — {vault.name}", "", f"- Status: {vault.status}", f"- Sections filled: {workspace['filled_sections']} of 15", f"- Open questions: {workspace['open_questions']}", "", "This is customer-provided setup input. Review it before changing any live AI configuration.", "", "## Calls", ""]
    if not workspace["calls"]:
        lines += ["No calls recorded.", ""]
    for call in workspace["calls"]:
        lines += [f"### {call['title']} — {call['date']}", call["summary"], "Attendees: " + ", ".join(call["attendees"])]
        if call["url"]:
            lines += ["Recording: " + call["url"]]
        lines += [""]
    for index, section in enumerate(workspace["sections"], 1):
        lines += [f"## {index}. {section['title']} ({section['key']})", f"State: {section['state']}; marked done: {'yes' if section['is_done'] else 'no'}", ""]
        if not section["entries"]:
            lines += ["Client said they do not have this." if section["state"] == "dont_have" else "_Empty._", ""]
        for entry in section["entries"]:
            provenance = entry["author_type"] + (" via " + entry["origin"] if entry["author_type"] == "agent" else "")
            if entry["source_date"]:
                provenance += ", " + entry["source_date"]
            if entry["confirmed_at"]:
                provenance += ", confirmed by client"
            if entry["client_body"] is not None:
                provenance += ", edited by client — this text takes priority"
            lines += [f"({provenance})", entry["body"] or ("(dictation without transcript)" if entry["kind"] == "audio" else "")]
            if entry["original_body"] is not None:
                lines += ["Original source text (superseded by the client's edit):", entry["original_body"]]
            if entry["url"]:
                lines += ["Link: " + entry["url"]]
            if entry["file_name"]:
                lines += [f"File: {entry['file_name']} ({entry['size']} bytes)", f"Customer sharing allowed: {'yes' if entry['allowed_for_ai_sharing'] else 'no'}"]
            if entry["send_when"]:
                lines += ["When to send: " + entry["send_when"]]
            lines += [""]
    lines += ["## Questions", ""]
    for question in workspace["questions"]:
        state = "answered " + question["answered_at"][:10] if question["answered_at"] else "open"
        lines += [f"- [{state}] ({question['section']}) {question['text']}"]
        if question["answer"]:
            lines += ["  Client answer: " + question["answer"]]
    return "\n".join(lines).rstrip() + "\n"


@transaction.atomic
def create_profile_snapshot(vault, actor=None):
    """Capture effective facts and their precedence for human review, without AI writes."""
    vault = Vault.objects.select_for_update().get(pk=vault.pk)
    facts = []
    for entry in vault.entries.all():
        precedence = 1 if entry.client_body is not None else (2 if entry.author_type == "client" else (3 if entry.confirmed_at else 4))
        facts.append({"entry_id": str(entry.pk), "section": entry.section, "body": entry.effective_body, "url": entry.url, "file_name": entry.file_name, "precedence": precedence, "source": entry.source_label, "confirmed": bool(entry.confirmed_at or entry.author_type == "client"), "allowed_for_ai_sharing": entry.allowed_for_ai_sharing, "send_when": entry.send_when})
    for q in vault.questions.filter(answered_at__isnull=False):
        facts.append({"question_id": str(q.pk), "section": q.section, "question": q.text, "body": q.answer, "precedence": 2, "source": "Client answer", "confirmed": True})
    body = {"schema_version": 1, "vault_id": str(vault.pk), "organization_id": str(vault.organization_id), "name": vault.name, "source_updated_at": _iso(vault.updated_at), "review_required": True, "live_configuration_updated": False, "facts": sorted(facts, key=lambda item: item["precedence"]), "open_questions": [question_payload(q) for q in vault.questions.filter(answered_at__isnull=True)]}
    return VaultProfileSnapshot.objects.create(vault=vault, body=body, created_by=_actor(actor))
