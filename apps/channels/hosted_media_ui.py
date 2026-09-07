"""Tenant-authenticated media proxy for Hosted WhatsApp messages."""

import base64
import binascii

from django.http import Http404, HttpResponse, JsonResponse
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from apps.organizations.features import is_hosted_account_enabled

from .models import WhatsAppAccount, WhatsAppMessage
from .providers.whatsapp_web import WhatsAppWebClient, WhatsAppWebGatewayError


MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_MEDIA_BYTES + 2) // 3) * 4 + 16


def _organization(request):
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization or not is_hosted_account_enabled(organization):
        raise Http404
    return organization


def _safe_content_type(value):
    content_type = str(value or "application/octet-stream").lower().split(";", 1)[0]
    if content_type == "image/svg+xml" or content_type in {"text/html", "application/xhtml+xml"}:
        return "application/octet-stream"
    if content_type.startswith(("image/", "audio/", "video/")):
        return content_type
    if content_type == "application/pdf" or content_type.startswith("application/"):
        return content_type
    return "application/octet-stream"


@crm_login_required
@require_GET
def hosted_message_media_view(request, account_id, message_id):
    organization = _organization(request)
    account = WhatsAppAccount.objects.filter(
        id=account_id,
        organization=organization,
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        raise Http404

    message = WhatsAppMessage.objects.filter(
        id=message_id,
        organization=organization,
        account=account,
    ).first()
    if not message:
        raise Http404

    raw_payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    has_media = bool(raw_payload.get("hasMedia")) or message.message_type in {
        WhatsAppMessage.MessageType.IMAGE,
        WhatsAppMessage.MessageType.AUDIO,
        WhatsAppMessage.MessageType.VIDEO,
        WhatsAppMessage.MessageType.DOCUMENT,
    }
    if not has_media:
        raise Http404

    external_id = str(message.external_id or "")
    if not external_id.startswith("wweb:"):
        raise Http404
    gateway_message_id = external_id[len("wweb:") :]
    if not gateway_message_id:
        raise Http404

    try:
        media = WhatsAppWebClient().download_media(
            session_id=account.id,
            message_id=gateway_message_id,
        )
    except WhatsAppWebGatewayError as exc:
        status = 404 if exc.status_code == 404 else 502
        return JsonResponse(
            {"ok": False, "error": str(exc)},
            status=status,
        )

    encoded = str(media.get("data") or "")
    if not encoded or len(encoded) > MAX_BASE64_CHARS:
        return JsonResponse(
            {"ok": False, "error": "Hosted media is unavailable or too large."},
            status=413 if encoded else 404,
        )

    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return JsonResponse(
            {"ok": False, "error": "Hosted media payload is invalid."},
            status=502,
        )

    if len(content) > MAX_MEDIA_BYTES:
        return JsonResponse(
            {"ok": False, "error": "Hosted media exceeds the 25 MB preview limit."},
            status=413,
        )

    content_type = _safe_content_type(media.get("mimetype"))
    filename = get_valid_filename(str(media.get("filename") or "")) or f"whatsapp-{message.id}"
    inline = content_type.startswith(("image/", "audio/", "video/")) or content_type == "application/pdf"

    response = HttpResponse(content, content_type=content_type)
    response["Content-Length"] = str(len(content))
    response["Content-Disposition"] = (
        f'{"inline" if inline else "attachment"}; filename="{filename}"'
    )
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response
