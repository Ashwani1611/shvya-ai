"""Shared presentation and form dispatch for the two Vault interfaces."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from . import services
from .models import VaultEntry
from .sections import SECTIONS


def private_response(response):
    response["Cache-Control"] = "private, no-store, max-age=0"
    # Native HTTPS forms need same-origin Origin/Referer headers for Django CSRF.
    # Keep private Vault URLs out of referrers sent to external sites.
    response["Referrer-Policy"] = "same-origin"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def workspace_context(request, vault, *, staff=False):
    sections = []
    state_map = {s.key: s for s in vault.sections.all()}
    entries = list(vault.entries.all())
    questions = list(vault.questions.all())
    for definition in SECTIONS:
        key = definition["key"]
        state = state_map.get(key)
        section_entries = []
        for entry in entries:
            if entry.section != key:
                continue
            item = {
                "id": entry.id, "kind": entry.kind,
                "body": entry.effective_body, "effective_body": entry.effective_body, "source_body": entry.body,
                "original_body": entry.body if entry.client_body is not None else "",
                "url": entry.url, "file_name": entry.file_name,
                "file_size": entry.file_size, "source_label": entry.source_label,
                "author_type": entry.author_type, "origin": entry.origin,
                "source_date": entry.source_date, "confirmed_at": entry.confirmed_at,
                "client_edited_at": entry.client_edited_at,
                "updated_at": entry.updated_at, "updated_at_iso": entry.updated_at.isoformat(),
                "can_edit": not staff or entry.author_type == VaultEntry.Author.TEAM,
                "can_delete": (staff and entry.author_type == VaultEntry.Author.TEAM) or (not staff and entry.author_type == VaultEntry.Author.CLIENT),
                "can_confirm": not staff and entry.author_type != VaultEntry.Author.CLIENT and not entry.confirmed_at,
                "allowed_for_ai_sharing": entry.allowed_for_ai_sharing,
                "send_when": entry.send_when,
                "transcription_status": entry.transcription_status,
            }
            if entry.file:
                item["download_url"] = reverse(
                    "vault-staff-file" if staff else "vault-client-file",
                    kwargs={"vault_id": vault.id, "entry_id": entry.id} if staff else {"slug": vault.slug, "entry_id": entry.id},
                )
            section_entries.append(item)
        sections.append({
            **definition, "what_to_add": [definition["guidance"]],
            "why": definition["description"], "is_optional": definition["group"] != "Recommended",
            "state": state.state if state else "empty", "status": state.state if state else "empty",
            "is_done": state.is_done if state else False,
            "entries": section_entries,
            "questions": [q for q in questions if q.section == key],
            "open_question_count": sum(q.section == key and q.answered_at is None for q in questions),
        })
    completed = sum(s["is_done"] or s["state"] == "dont_have" for s in sections)
    filled = sum(s["state"] == "filled" for s in sections)
    calls = [{"id": call.id, "title": call.title, "date": call.date,
              "url": call.url if (staff or call.share_recording) else "",
              "duration_min": call.duration_min, "attendees": call.attendees,
              "summary": call.summary, "share_recording": call.share_recording}
             for call in vault.calls.all()]
    private_url = request.build_absolute_uri(reverse("vault-client", kwargs={"slug": vault.slug}))
    context = {
        "vault": vault, "is_staff": staff, "base_template": "superadmin/base.html" if staff else "vault/client_base.html",
        "action_url": request.path, "sections": sections, "calls": calls, "questions": questions,
        "entry_count": len(entries),
        "storage_display": _display_bytes(vault.storage_used_bytes),
        "quota_display": _display_bytes(vault.storage_quota_bytes),
        "filled_count": filled, "completed_count": completed,
        "progress": round(completed * 100 / 15), "private_url": private_url,
        "open_questions": sum(q.answered_at is None for q in questions),
        "list_url": reverse("vault-staff-list"),
        "download_url": reverse("vault-staff-export", kwargs={"vault_id": vault.id}) if staff else reverse("vault-client-export", kwargs={"slug": vault.slug}),
        "workspace_url": reverse("vault-staff-detail", kwargs={"vault_id": vault.id}) if staff else reverse("vault-client", kwargs={"slug": vault.slug}),
        "max_upload_mb": min(50, getattr(settings, "VAULT_MAX_FILE_BYTES", 25 * 1024 * 1024) // (1024 * 1024)),
    }
    if staff:
        snapshot = vault.profile_snapshots.first()
        context["snapshot"] = snapshot
        context["snapshot_download_url"] = reverse("vault-staff-profile", kwargs={"vault_id": vault.id, "snapshot_id": snapshot.id}) if snapshot else ""
    return context


def apply_workspace_action(request, vault, *, staff=False):
    """Untrusted forms cannot choose their author, scope or confirmation status."""
    data = request.POST
    action = data.get("action", "")
    actor = request.user if staff else None
    if action == "add_entry":
        payload = {key: data.get(key, "") for key in ("section", "kind", "body", "url", "send_when")}
        payload["allowed_for_ai_sharing"] = data.get("allowed_for_ai_sharing") == "on"
        payload["origin"] = (data.get("origin") or "ops_chat") if staff else "other"
        if staff:
            payload["source_date"] = data.get("source_date") or None
        services.upsert_entry(vault, payload, author_type="team" if staff else "client", actor=actor, upload=request.FILES.get("file"))
    elif action == "update_entry":
        entry = _entry(vault, data.get("entry_id"))
        if staff:
            if entry.author_type != "team":
                raise PermissionDenied("Client and agent entries retain their original authorship.")
            expected = data.get("expected_updated_at")
            if expected and parse_datetime(expected) != entry.updated_at:
                raise ValidationError("This entry changed. Reload it before saving your edit.")
            # Staff edits use a scoped external ID and never overwrite a client override.
            payload = {"section": entry.section, "kind": entry.kind, "body": data.get("body", ""),
                       "url": entry.url, "origin": entry.origin, "external_id": entry.external_id or f"team:{entry.id}",
                       "source_date": entry.source_date,
                       "send_when": entry.send_when, "allowed_for_ai_sharing": entry.allowed_for_ai_sharing}
            if not entry.external_id:
                entry.external_id = payload["external_id"]
                entry.save(update_fields=["external_id"])
            services.upsert_entry(vault, payload, author_type="team", actor=actor)
        else:
            services.edit_client_entry(vault, entry.id, data.get("body", ""), expected_updated_at=data.get("expected_updated_at") or None)
    elif action == "confirm_entry" and not staff:
        services.confirm_entry(vault, _entry(vault, data.get("entry_id")).id)
    elif action == "delete_entry":
        entry = _entry(vault, data.get("entry_id"))
        if entry.author_type != ("team" if staff else "client"):
            raise PermissionDenied("Only your own entries can be removed.")
        services.delete_entry(vault, entry.id, actor_type="team" if staff else "client", actor=actor)
    elif action == "set_section":
        services.set_section_state(vault, data.get("section", ""), state=data.get("state") or None,
                                   is_done=data.get("is_done", "") in {"true", "1", "on"}, actor_type="team" if staff else "client")
    elif action == "ask_question" and staff:
        services.upsert_question(vault, {"section": data.get("section", "other"), "text": data.get("text", "")}, author_type="team")
    elif action == "answer_question" and not staff:
        from django.shortcuts import get_object_or_404
        question = get_object_or_404(vault.questions, pk=data.get("question_id"))
        services.answer_question(vault, question.id, data.get("answer", ""))
    elif action == "add_call" and staff:
        services.upsert_call(vault, {"title": data.get("title", ""), "date": data.get("date", ""),
            "url": data.get("url", ""), "duration_min": int(data["duration_min"]) if data.get("duration_min") else None,
            "attendees": [s.strip() for s in data.get("attendees", "").splitlines() if s.strip()],
            "summary": data.get("summary", ""), "share_recording": data.get("share_recording") == "on"})
    elif action == "submit" and not staff:
        services.submit_vault(vault)
    elif action == "profile_snapshot" and staff:
        services.create_profile_snapshot(vault, actor)
    else:
        raise ValidationError("This action is not available.")
    return "Submitted for review." if action == "submit" else "Saved."


def _entry(vault, entry_id):
    from django.shortcuts import get_object_or_404
    try:
        return get_object_or_404(vault.entries, pk=entry_id)
    except (ValueError, ValidationError):
        raise ValidationError("Choose a valid entry.")


def export_filename(vault, suffix="md"):
    return f"shvya-vault-{slugify(vault.name)[:70] or 'client'}-{timezone.localdate().isoformat()}.{suffix}"


def _display_bytes(value):
    return f"{value / 1024 ** 3:.1f} GB" if value >= 1024 ** 3 else f"{value / 1024 ** 2:.1f} MB"
