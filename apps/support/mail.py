"""Verified-sender email ingestion. A From: header by itself never grants access."""
from __future__ import annotations

import hashlib
import imaplib
import re
import ssl
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from .access import customer_authorized, visible_tickets
from .models import InboundReceipt, SupportSettings, TicketIssue, TicketPriority
from .policy import REFERENCE_RE, normalized_email, platform_staff, html_email_text
from .services import create_ticket, reply


class RejectedEmail(ValueError):
    """Terminal policy rejection (safe reason only, no message body)."""


def parsed_sender(message):
    addresses = [email for _, email in getaddresses(message.get_all("From", []))]
    if len(addresses) != 1:
        raise RejectedEmail("ambiguous_sender")
    try:
        return normalized_email(addresses[0])
    except ValueError as exc:
        raise RejectedEmail("invalid_sender") from exc


def _text_and_files(message):
    text = message.get_body(preferencelist=("plain", "html"))
    if text is None:
        raise RejectedEmail("message_body_required")
    body = text.get_content().strip()
    if text.get_content_subtype() == "html":
        body = html_email_text(body)
    if not body or len(body) > 30000:
        raise RejectedEmail("invalid_body_length")
    files = []
    for part in message.iter_attachments():
        if not part.get_filename():
            continue
        raw = part.get_payload(decode=True)
        if raw:
            files.append(SimpleUploadedFile(part.get_filename(), raw, content_type="application/octet-stream"))
    return body, files


def ingest_verified_email(raw: bytes, *, verified_sender: str):
    """verified_sender must come from a trusted mail gateway, not a request header.

    The optional trusted-IMAP adapter below is the external caller. This is
    not an HTTP endpoint; callers must supply an independently verified identity.
    """
    max_bytes = int(getattr(settings, "SUPPORT_MAX_EMAIL_BYTES", 35*1024*1024))
    if not raw or len(raw) > max_bytes:
        raise RejectedEmail("message_too_large")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parsed_sender(message)
    if sender != normalized_email(verified_sender):
        raise RejectedEmail("sender_identity_mismatch")
    config = SupportSettings.load()
    if not config.email_intake_enabled:
        raise RejectedEmail("email_intake_disabled")
    subject = str(message.get("Subject", "")).replace("\r", " ").replace("\n", " ")
    if str(message.get("Auto-Submitted", "no")).lower() != "no" or str(message.get("Precedence", "")).lower() in ("bulk", "junk", "list"):
        raise RejectedEmail("automated_message")
    block = {s.strip().lower() for s in config.blocked_senders.splitlines() if s.strip()}
    if sender in block or "@"+sender.split("@", 1)[1] in block:
        raise RejectedEmail("blocked_sender")
    if any(term.strip().lower() in subject.lower() for term in config.blocked_subject_terms.splitlines() if term.strip()):
        raise RejectedEmail("blocked_subject")
    matches = list(get_user_model().objects.select_related("organization").filter(email__iexact=sender)[:2])
    if len(matches) != 1:
        raise RejectedEmail("unique_registered_identity_required")
    actor = matches[0]
    if not (customer_authorized(actor) or platform_staff(actor)):
        raise RejectedEmail("registered_active_user_required")
    ids = message.get_all("Message-ID", [])
    if len(ids) != 1 or not re.fullmatch(r"<[^\s<>]{1,450}>", str(ids[0]).strip()):
        raise RejectedEmail("valid_message_id_required")
    # Include verified identity to prevent another sender squatting an idempotency key.
    digest = hashlib.sha256((sender+"\n"+str(ids[0]).strip()).encode()).hexdigest()
    refs = set(REFERENCE_RE.findall(subject.upper()))
    if len(refs) > 1:
        raise RejectedEmail("ambiguous_ticket_reference")
    body, files = _text_and_files(message)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=actor.pk)
        prior = InboundReceipt.objects.filter(pk=digest).first()
        if prior:
            return prior
        if refs:
            ticket = visible_tickets(actor).filter(reference=next(iter(refs))).first()
            if ticket is None:
                raise RejectedEmail("ticket_not_accessible")
            ticket = reply(ticket_id=ticket.pk, actor=actor, body=body, files=files)
        else:
            if config.email_replies_only or platform_staff(actor):
                raise RejectedEmail("new_email_tickets_disabled")
            # Defaults are configured relationships, not mutable display names.
            issue = TicketIssue.objects.select_related("category").filter(pk=config.email_issue_id,
                active=True, category__active=True).first()
            priority = TicketPriority.objects.filter(pk=config.email_priority_id, active=True).first()
            if not issue or not priority:
                raise RejectedEmail("email_defaults_unavailable")
            category = issue.category
            ticket = create_ticket(actor=actor, category_id=category.pk, issue_id=issue.pk,
                priority_id=priority.pk, subject=subject or "Email support request", body=body, files=files, source="email")
        return InboundReceipt.objects.create(digest=digest, ticket=ticket, result="accepted")


def trusted_imap_sender(message):
    """Fail closed unless the deployment explicitly trusts its receiving MTA.

    Operational prerequisite: the receiving mail server MUST strip any incoming
    Authentication-Results using its authserv-id and inject its own result first.
    Without that guarantee leave email intake disabled. Do not trust an arbitrary
    Authentication-Results header or expose ingest_verified_email as an unsigned endpoint.
    """
    authserv = getattr(settings, "SUPPORT_IMAP_AUTHSERV_ID", "").lower()
    if not getattr(settings, "SUPPORT_IMAP_TRUST_RECEIVER", False) or not authserv:
        raise RejectedEmail("trusted_receiving_mta_required")
    results = message.get_all("Authentication-Results", [])
    if not results:
        raise RejectedEmail("sender_authentication_missing")
    header = str(results[0]).lower()
    if header.split(";", 1)[0].strip() != authserv:
        raise RejectedEmail("untrusted_authentication_result")
    sender = parsed_sender(message)
    domain = sender.rsplit("@", 1)[1]
    # DMARC alignment must apply to the actual From domain, not a substring.
    matches = re.findall(r"(?:^|;)\s*dmarc=pass\b[^;]*?\bheader\.from=([^;\s]+)", header)
    if domain not in [v.strip('"').rstrip('.') for v in matches]:
        raise RejectedEmail("sender_authentication_failed")
    return sender


def _record_rejection(client, uid, *, host, user, folder, validity, reason):
    # A durable, content-free quarantine record prevents a bad UNSEEN message
    # from starving the mailbox. Flag it for operator review, never delete it.
    key = "\n".join((host, user, folder, validity, uid.decode("ascii")))
    InboundReceipt.objects.get_or_create(digest=hashlib.sha256(("imap-quarantine:"+key).encode()).hexdigest(),
        defaults={"result": "rejected", "reason": reason[:100]})
    status, _ = client.uid("store", uid, "+FLAGS.SILENT", "(\\Seen \\Flagged)")
    if status != "OK":
        raise RuntimeError("SupportMailboxAcknowledgementFailed")


def poll_mailbox(*, limit=25):
    if not SupportSettings.load().email_intake_enabled:
        return {"imported": 0, "rejected": 0, "disabled": True}
    host = getattr(settings, "SUPPORT_IMAP_HOST", "")
    user = getattr(settings, "SUPPORT_IMAP_USER", "")
    password = getattr(settings, "SUPPORT_IMAP_PASSWORD", "")
    if not all((host, user, password)):
        return {"imported": 0, "rejected": 0, "configuration_required": True}
    if not getattr(settings, "SUPPORT_IMAP_TRUST_RECEIVER", False):
        return {"imported": 0, "rejected": 0, "verification_required": True}
    imported = rejected = 0
    with imaplib.IMAP4_SSL(host, port=int(getattr(settings, "SUPPORT_IMAP_PORT", 993)),
                          ssl_context=ssl.create_default_context(), timeout=20) as client:
        client.login(user, password)
        folder = getattr(settings, "SUPPORT_IMAP_FOLDER", "INBOX")
        status, _ = client.select(folder)
        if status != "OK":
            raise RuntimeError("SupportMailboxUnavailable")
        _, validity_data = client.response("UIDVALIDITY")
        validity = (validity_data[0] or b"").decode("ascii") if validity_data else ""
        if not validity:
            raise RuntimeError("SupportMailboxUIDValidityMissing")
        status, result = client.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("SupportMailboxSearchFailed")
        for uid in (result[0] or b"").split()[:limit]:
            status, size_data = client.uid("fetch", uid, "(RFC822.SIZE)")
            size_match = re.search(rb"RFC822.SIZE\s+(\d+)", b" ".join(x for x in size_data if isinstance(x, bytes)))
            if status != "OK" or not size_match:
                continue
            if int(size_match[1]) > int(getattr(settings, "SUPPORT_MAX_EMAIL_BYTES", 35*1024*1024)):
                _record_rejection(client, uid, host=host, user=user, folder=folder,
                    validity=validity, reason="message_too_large")
                rejected += 1
                continue  # Never download an unbounded blob.
            status, data = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = next((x[1] for x in data if isinstance(x, tuple)), b"")
            try:
                message = BytesParser(policy=policy.default).parsebytes(raw)
                sender = trusted_imap_sender(message)
                ingest_verified_email(raw, verified_sender=sender)
            except (RejectedEmail, ValidationError, UnicodeError, LookupError) as exc:
                reason = str(exc) if isinstance(exc, RejectedEmail) else "message_validation_failed"
                _record_rejection(client, uid, host=host, user=user, folder=folder,
                    validity=validity, reason=reason)
                rejected += 1
                continue
            # Only acknowledge after the ticket/receipt transaction has committed.
            status, _ = client.uid("store", uid, "+FLAGS.SILENT", "(\\Seen)")
            if status != "OK":
                raise RuntimeError("SupportMailboxAcknowledgementFailed")
            imported += 1
    return {"imported": imported, "rejected": rejected}
