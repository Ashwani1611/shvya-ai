"""Recipient access to a file explicitly queued by a validated Instagram turn."""
from pathlib import Path
from uuid import UUID

from django.core import signing
from django.http import FileResponse, Http404
from django.views.decorators.http import require_safe

from apps.ai_engagement.services.file_sharing import FileSharingError
from apps.ai_engagement.services.instagram_files import (
    SHARED_FILE_MAX_AGE,
    SHARED_FILE_SALT,
    PROVIDER_FILE_SALT,
    PROVIDER_FILE_MAX_AGE,
    document_for_message,
)
from apps.channels.instagram_models import InstagramMessage


@require_safe
def instagram_shared_file(request, token, *, provider_fetch=False):
    try:
        message_id = UUID(signing.TimestampSigner(salt=PROVIDER_FILE_SALT if provider_fetch else SHARED_FILE_SALT).unsign(
            token, max_age=PROVIDER_FILE_MAX_AGE if provider_fetch else SHARED_FILE_MAX_AGE,
        ))
    except (signing.BadSignature, ValueError, TypeError):
        raise Http404 from None
    # The unforgeable, expiring token grants access to this exact send. Tenant
    # scope is derived from that row; the caller never supplies an organization.
    message = InstagramMessage.objects.select_related(
        "organization", "account", "conversation",
    ).filter(
        pk=message_id, direction=InstagramMessage.Direction.OUTBOUND,
        status__in=([InstagramMessage.Status.QUEUED, InstagramMessage.Status.FAILED,
                     InstagramMessage.Status.SENT, InstagramMessage.Status.READ]
                    if provider_fetch else [InstagramMessage.Status.SENT, InstagramMessage.Status.READ]),
    ).first()
    if message is None:
        raise Http404
    try:
        document = document_for_message(message)
        if provider_fetch and Path(document.file.name).suffix.lower() != ".pdf":
            raise Http404
        response = FileResponse(
            document.file.open("rb"), as_attachment=True,
            filename=Path(document.file.name).name,
        )
    except (FileSharingError, FileNotFoundError, OSError, ValueError):
        raise Http404 from None
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    return response
