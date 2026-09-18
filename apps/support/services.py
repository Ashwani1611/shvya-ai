"""One execution contract for browser, shared-link, email and worker mutations."""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.module_loading import import_string

from .access import allowed_pipelines, customer_authorized, staff_users, visible_tickets
from .fields import validate_custom
from .models import (Attachment, SharedAccess, SupportSettings, Ticket, TicketCategory,
                     TicketIssue, TicketMessage, TicketPriority, TicketStatus, WorkItem)
from .notifications import record_event
from .policy import after_reply, digest_token, platform_staff, safe_source_path, validate_file
from .storage import private_storage


logger = logging.getLogger(__name__)


class Conflict(ValidationError):
    pass


def validate_text(text, *, maximum=30000):
    text = str(text or "").strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValidationError(f"Enter text between 1 and {maximum:,} characters.")
    return text


def canonical(ticket_id, actor):
    ticket = get_object_or_404(visible_tickets(actor), pk=ticket_id)
    if ticket.merged_into_id:
        ticket = get_object_or_404(visible_tickets(actor), pk=ticket.merged_into_id, merged_into__isnull=True)
    return ticket


def check_grant(grant, *, reply=False, close=False):
    fresh = SharedAccess.objects.select_related("ticket__organization", "ticket__requester").filter(pk=grant.pk).first()
    if (not fresh or fresh.revoked_at or fresh.expires_at <= timezone.now()
            or fresh.ticket.merged_into_id or not fresh.ticket.organization.is_active
            or not fresh.ticket.requester.is_active):
        raise PermissionDenied("This ticket link has expired or was revoked.")
    if reply and not fresh.can_reply:
        raise PermissionDenied("This link is read-only.")
    if close and not fresh.can_close:
        raise PermissionDenied("This link cannot change ticket status.")
    return fresh


def resolve_grant(raw_token):
    if not isinstance(raw_token, str) or not 32 <= len(raw_token) <= 128:
        raise PermissionDenied("Invalid ticket link.")
    grant = SharedAccess.objects.filter(token_hash=digest_token(raw_token)).first()
    if not grant:
        raise PermissionDenied("Invalid ticket link.")
    return check_grant(grant)


def lock_ticket(ticket_id, actor=None, grant=None):
    """Caller must be atomic. Recheck a share after acquiring the ticket lock."""
    if grant:
        ticket = Ticket.objects.select_for_update().get(pk=grant.ticket_id)
        check_grant(grant)
        if str(ticket.pk) != str(ticket_id):
            raise PermissionDenied("Invalid ticket context.")
    else:
        candidate = canonical(ticket_id, actor)
        ticket = get_object_or_404(visible_tickets(actor).select_for_update(), pk=candidate.pk)
    if ticket.merged_into_id:
        raise Conflict("This ticket was just merged. Refresh before trying again.")
    return ticket


def check_version(ticket, version):
    if version is not None and ticket.version != int(version):
        raise Conflict("Another person updated this ticket. Refresh to see their changes before saving.")


def touch(ticket):
    ticket.updated_at = timezone.now()
    ticket.version += 1
    ticket.full_clean()
    ticket.save()


@contextmanager
def file_transaction():
    """Clean up file writes when this transaction fails; recovery also removes old orphans."""
    written = []
    try:
        with transaction.atomic():
            yield written
    except Exception:
        for name in written:
            try:
                private_storage.delete(name)
            except OSError:
                # Private orphan cleanup is a separate, grace-period maintenance task.
                logger.warning("Support upload rollback cleanup failed; private orphan retained for maintenance.")
        raise


def validate_uploads(files):
    files = list(files or [])
    config = SupportSettings.load()
    if len(files) > config.max_files:
        raise ValidationError(f"Attach at most {config.max_files} files per message.")
    if sum(f.size for f in files) > config.max_total_mb * 1024 * 1024:
        raise ValidationError("The attachments exceed the total upload limit.")
    for upload in files:
        try:
            upload.name = validate_file(upload.name, upload.size, config.allowed_extensions,
                                        config.max_file_mb * 1024 * 1024)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        scanner_path = getattr(settings, "SUPPORT_ATTACHMENT_SCANNER", "")
        if scanner_path:
            try:
                passed = import_string(scanner_path)(upload) is True
            except Exception as exc:
                logger.warning("Support attachment scanner unavailable (%s).", type(exc).__name__)
                raise ValidationError("Attachment scanning could not complete. Try again without the file.") from exc
            finally:
                upload.seek(0)
            if not passed:
                raise ValidationError("The attachment did not pass the configured security scan.")
        elif getattr(settings, "SUPPORT_REQUIRE_SCANNER", False):
            raise ValidationError("Attachment scanning is unavailable. Contact support without the file.")
    return files


def store_files(message, files, written):
    for uploaded in files:
        digest = hashlib.sha256()
        size = 0
        for chunk in uploaded.chunks():
            size += len(chunk)
            digest.update(chunk)
        if size != uploaded.size:
            raise ValidationError("Attachment size changed during upload.")
        uploaded.seek(0)
        row = Attachment(message=message, original_name=uploaded.name, size=size, sha256=digest.hexdigest())
        row.file.save(uploaded.name, uploaded, save=False)
        written.append(row.file.name)
        row.save()


def create_ticket(*, actor, category_id, issue_id, priority_id, subject, body,
                  pipeline_id=None, custom=None, files=(), source="portal",
                  source_path="", client_key=None):
    if not customer_authorized(actor):
        raise PermissionDenied("Only active organization users can open support tickets.")
    subject = validate_text(subject, maximum=200)
    body = validate_text(body)
    key = uuid.UUID(str(client_key)) if client_key else None
    uploads = validate_uploads(files)
    with file_transaction() as written:
        # Serializes per-requester creation, enforcing the same retry and rate policy across channels.
        get_user_model().objects.select_for_update().get(pk=actor.pk)
        if key:
            existing = Ticket.objects.filter(requester=actor, submission_key=key).first()
            if existing:
                return existing
        config = SupportSettings.load()
        if Ticket.objects.filter(requester=actor, created_at__gte=timezone.now()-timedelta(hours=1)).count() >= config.new_tickets_per_hour:
            raise ValidationError("You have reached the hourly ticket limit. Reply to an existing ticket instead.")
        category = get_object_or_404(TicketCategory, pk=category_id, active=True)
        issue = get_object_or_404(TicketIssue, pk=issue_id, category=category, active=True)
        priority = get_object_or_404(TicketPriority, pk=priority_id, active=True)
        pipeline = get_object_or_404(allowed_pipelines(actor), pk=pipeline_id) if pipeline_id else None
        ticket = Ticket(
            organization=actor.organization, requester=actor, pipeline=pipeline,
            subject=subject, category=category, issue=issue, priority=priority,
            status=TicketStatus.objects.get(key="open", system=True, active=True),
            source=source, submission_key=key,
            custom_values=validate_custom(custom),
            context={"organization_name": actor.organization.name,
                     "requester_name": actor.name, "requester_email": actor.email,
                     "pipeline_name": pipeline.name if pipeline else "Not pipeline-specific",
                     "source_path": safe_source_path(source_path)},
        )
        ticket.full_clean()
        ticket.save()
        message = TicketMessage.objects.create(ticket=ticket, origin_ticket=ticket, author=actor,
                    author_name=actor.name or "Organization user", author_kind="customer", body=body)
        store_files(message, uploads, written)
        record_event(ticket, actor, "created")
        return ticket


def reply(*, ticket_id, actor=None, grant=None, body, internal=False, files=(),
          status_id=None, assign_me=False, client_key=None):
    staff = platform_staff(actor)
    if actor is None and grant is None:
        raise PermissionDenied()
    if internal and not staff:
        raise PermissionDenied("Internal notes are restricted to Shvya-Ops.")
    body = validate_text(body)
    uploads = validate_uploads(files)
    key = uuid.UUID(str(client_key)) if client_key else None
    with file_transaction() as written:
        ticket = lock_ticket(ticket_id, actor, grant)
        if grant:
            check_grant(grant, reply=True)
        if key:
            prior = TicketMessage.objects.filter(ticket=ticket, author=actor, client_key=key).first()
            if prior:
                return ticket
        if grant and ticket.messages.filter(author__isnull=True,
                created_at__gte=timezone.now()-timedelta(hours=1)).count() >= 60:
            raise ValidationError("The shared-link hourly reply limit has been reached.")
        config = SupportSettings.load()
        name = (actor.name or ("Shvya-Ops" if staff else "Organization user")) if actor else grant.label
        message = TicketMessage.objects.create(ticket=ticket, origin_ticket=ticket, author=actor,
            author_name=name, author_kind="staff" if staff else ("shared" if grant else "customer"),
            internal=internal, body=body, client_key=key)
        store_files(message, uploads, written)
        if internal:
            touch(ticket)
            record_event(ticket, actor, "internal_note")
            return ticket
        now = timezone.now()
        old_assignee = ticket.assignee_id
        if staff:
            first = ticket.first_staff_reply_at is None
            if first:
                ticket.first_staff_reply_at = now
            if assign_me or (first and config.auto_assign_first_reply and ticket.assignee_id is None):
                ticket.assignee = actor
            ticket.status = (get_object_or_404(TicketStatus, pk=status_id, active=True) if status_id
                             else TicketStatus.objects.get(key="answered", system=True))
            if ticket.status.behavior == "closed":
                ticket.custom_values = validate_custom({}, staff=True, existing=ticket.custom_values,
                                                        closing=True, partial=True)
        else:
            if after_reply(ticket.status.behavior, staff=False) == "open":
                ticket.status = TicketStatus.objects.get(key="open", system=True)
        ticket.last_public_activity_at = now
        touch(ticket)
        record_event(ticket, actor, "staff_reply" if staff else "customer_reply")
        if old_assignee != ticket.assignee_id:
            record_event(ticket, actor, "assigned", {"assignee": str(ticket.assignee_id or "")})
        return ticket


def update_ticket(*, ticket_id, actor=None, grant=None, status_id=None, priority_id=None,
                  assignee_id=None, change_assignee=False, category_id=None, issue_id=None,
                  custom=None, version=None):
    staff = platform_staff(actor)
    with transaction.atomic():
        ticket = lock_ticket(ticket_id, actor, grant)
        check_version(ticket, version)
        if not staff:
            if grant:
                check_grant(grant, close=True)
            if not SupportSettings.load().allow_customer_close:
                raise PermissionDenied("Customer status changes are disabled.")
            if priority_id or change_assignee or category_id or issue_id or custom is not None:
                raise PermissionDenied("These properties are managed by Shvya-Ops.")
        old_status, old_assignee = ticket.status_id, ticket.assignee_id
        if status_id:
            status = get_object_or_404(TicketStatus, pk=status_id, active=True)
            if not staff and (not status.system or status.key not in ("open", "closed")):
                raise PermissionDenied("Customers can only close or reopen their tickets.")
            ticket.status = status
        if staff:
            if priority_id:
                ticket.priority = get_object_or_404(TicketPriority, pk=priority_id, active=True)
            if change_assignee:
                ticket.assignee = get_object_or_404(staff_users(), pk=assignee_id) if assignee_id else None
            if category_id:
                ticket.category = get_object_or_404(TicketCategory, pk=category_id, active=True)
            if issue_id:
                ticket.issue = get_object_or_404(TicketIssue, pk=issue_id, category=ticket.category, active=True)
            ticket.custom_values = validate_custom(custom, staff=True, existing=ticket.custom_values, partial=True,
                                                  closing=ticket.status.behavior == "closed")
        elif ticket.status.behavior == "closed":
            validate_custom({}, existing=ticket.custom_values, closing=True, partial=True)
        touch(ticket)
        if old_status != ticket.status_id:
            record_event(ticket, actor, "status_changed", {"status": ticket.status.name})
        if old_assignee != ticket.assignee_id:
            record_event(ticket, actor, "assigned", {"assignee": str(ticket.assignee_id or "")})
        record_event(ticket, actor, "properties_updated")
        return ticket


@transaction.atomic
def merge_tickets(*, actor, primary_id, source_ids):
    if not platform_staff(actor):
        raise PermissionDenied()
    ids = {uuid.UUID(str(value)) for value in source_ids}
    primary_id = uuid.UUID(str(primary_id))
    if not ids or len(ids) > 20 or primary_id in ids:
        raise ValidationError("Select 1–20 other tickets to merge.")
    tickets = list(Ticket.objects.select_for_update().filter(pk__in=ids | {primary_id}).order_by("id"))
    if len(tickets) != len(ids) + 1:
        raise ValidationError("A selected ticket no longer exists.")
    primary = next(t for t in tickets if t.pk == primary_id)
    sources = [t for t in tickets if t.pk != primary_id]
    if any(t.merged_into_id for t in tickets):
        raise ValidationError("A selected ticket was already merged. Refresh the selection.")
    if any(t.organization_id != primary.organization_id or t.requester_id != primary.requester_id for t in sources):
        raise ValidationError("Merge only tickets from the same organization and requester.")
    if Ticket.objects.filter(merged_into_id__in=ids).exists():
        raise ValidationError("Keep an existing primary ticket as the primary; nested merges are not supported.")
    closed = TicketStatus.objects.get(key="closed", system=True)
    now = timezone.now()
    for source in sources:
        # Only public messages move; attachment FKs follow their message, with no file copy/deletion.
        # Retry keys are scoped to their original ticket; do not collide with a
        # different message's key on the primary. Email receipts remain durable.
        source.messages.filter(internal=False).update(ticket=primary, client_key=None)
        source.status, source.merged_into = closed, primary
        touch(source)
        record_event(source, actor, "merged_source", {"primary": primary.reference})
    SharedAccess.objects.filter(ticket_id__in=ids | {primary_id}, revoked_at__isnull=True).update(revoked_at=now)
    latest = primary.messages.filter(internal=False).order_by("-created_at").first()
    primary.last_public_activity_at = latest.created_at if latest else now
    first_staff = primary.messages.filter(internal=False, author_kind="staff").order_by("created_at").first()
    if first_staff:
        primary.first_staff_reply_at = first_staff.created_at
    touch(primary)
    record_event(primary, actor, "merged", {"sources": [s.reference for s in sources]})
    return primary


@transaction.atomic
def issue_share(*, actor, ticket_id, label, days=7, can_reply=False, can_close=False):
    ticket = lock_ticket(ticket_id, actor)
    if not platform_staff(actor) and actor.role != "admin" and actor.pk != ticket.requester_id:
        raise PermissionDenied("Only the requester, organization admin or Shvya-Ops can share a ticket.")
    if not 1 <= int(days) <= 30:
        raise ValidationError("Ticket links may last 1–30 days.")
    raw = secrets.token_urlsafe(48)
    grant = SharedAccess.objects.create(ticket=ticket, token_hash=digest_token(raw),
        label=validate_text(label or "Shared viewer", maximum=80), can_reply=bool(can_reply),
        can_close=bool(can_close), expires_at=timezone.now()+timedelta(days=int(days)), created_by=actor)
    record_event(ticket, actor, "link_created", {"link_id": str(grant.pk), "reply": grant.can_reply})
    return grant, raw


@transaction.atomic
def revoke_share(*, actor, ticket_id, grant_id):
    ticket = lock_ticket(ticket_id, actor)
    grant = get_object_or_404(SharedAccess, ticket=ticket, pk=grant_id)
    if not platform_staff(actor) and actor.role != "admin" and actor.pk != grant.created_by_id:
        raise PermissionDenied()
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    record_event(ticket, actor, "link_revoked", {"link_id": str(grant.pk)})


@transaction.atomic
def add_work_item(*, actor, ticket_id, kind, title, due_at, assignee_id):
    if not platform_staff(actor):
        raise PermissionDenied()
    ticket = lock_ticket(ticket_id, actor)
    item = WorkItem(ticket=ticket, kind=kind, title=validate_text(title, maximum=200), due_at=due_at,
                    assigned_to=get_object_or_404(staff_users(), pk=assignee_id))
    item.full_clean()
    item.save()
    record_event(ticket, actor, "work_created", {"kind": item.kind, "item": item.pk})
    return item


@transaction.atomic
def complete_work_item(*, actor, ticket_id, item_id):
    if not platform_staff(actor):
        raise PermissionDenied()
    ticket = get_object_or_404(visible_tickets(actor).select_for_update(), pk=ticket_id)
    item = get_object_or_404(WorkItem.objects.select_for_update(), pk=item_id, ticket=ticket)
    item.completed_at = timezone.now()
    item.save(update_fields=["completed_at"])
    record_event(ticket, actor, "work_completed", {"item": item.pk})
    return item
