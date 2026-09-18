"""Safe, lossless presentation of Meta Instagram messaging content.

Raw webhook/Graph payloads remain in the existing tenant-scoped data model.
Account tokens stay encrypted in their existing model field. This module exposes only display fields, never arbitrary provider JSON.
It deliberately does not download URLs or invent media that Meta did not return.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit
import re

POLICY_URL = "https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/"
WINDOW_HOURS = 24


def safe_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        return ""
    value = value.strip()
    if any(ord(char) < 32 for char in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    # Token-bearing Graph endpoints must never be exposed as browser media.
    if re.search(r"(?:[?&])(?:access_token|client_secret|appsecret_proof)=", value, re.I):
        return ""
    return value


def safe_error(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(r"(?i)(access_token|client_secret|appsecret_proof|authorization_code)([\s\"':=]+)[^\s&,\"'}]+", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"\b(?:IGQV|IGAA|EAA)[A-Za-z0-9_\-]{16,}\b", "[redacted]", text)
    return text[:1000]


def attachment_list(value: object) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("data", [value] if value else [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def display_attachments(attachments: object, raw_payload: object = None) -> list[dict]:
    raw = _object(raw_payload)
    message = _object(raw.get("message")) or raw
    if message.get("is_deleted"):
        return []
    result = []
    source = attachment_list(attachments) or attachment_list(message.get("attachments"))
    reply = _object(message.get("reply_to"))
    story = _object(reply.get("story"))
    if story:
        source = [{"type": "story_reply", "payload": story}] + source
    for item in source:
        payload = _object(item.get("payload"))
        image = _object(item.get("image_data"))
        video = _object(item.get("video_data"))
        audio = _object(item.get("audio_data"))
        kind = str(item.get("type") or item.get("mime_type") or "").lower()
        if image and not kind:
            kind = "image"
        elif video and not kind:
            kind = "video"
        elif audio and not kind:
            kind = "audio"
        url = next((candidate for value in (
            payload.get("url"), item.get("url"), video.get("url"),
            audio.get("url"), image.get("animated_gif_url"), image.get("url"), item.get("file_url"),
            payload.get("link"), item.get("link"),
        ) if (candidate := safe_url(value))), "")
        preview = next((candidate for value in (
            image.get("preview_url"), image.get("url"), video.get("preview_url"),
            payload.get("preview_url"), payload.get("image_url"), item.get("image_url"),
        ) if (candidate := safe_url(value))), "")
        if kind.startswith("image/") or kind in {"image", "animated_image", "gif", "sticker"}:
            display_kind, label = "image", "Sticker" if kind == "sticker" else "Image"
        elif kind.startswith("video/") or kind == "video":
            display_kind, label = "video", "Video"
        elif kind.startswith("audio/") or kind == "audio":
            display_kind, label = "audio", "Audio"
        else:
            display_kind = "link"
            label = {
                "story_reply": "Reply to story", "story_mention": "Story mention",
                "ig_reel": "Instagram reel", "reel": "Instagram reel",
                "ig_post": "Instagram post", "share": "Shared Instagram content",
                "file": "Attachment", "fallback": "Shared Instagram content",
            }.get(kind, "Instagram attachment")
        # A share may only expose a permalink, not downloadable video bytes.
        # Never guess a media type by the URL suffix or iframe untrusted content.
        result.append({"kind": display_kind, "label": label, "url": url,
                       "preview_url": preview, "unavailable": not bool(url or preview)})
    if message.get("is_unsupported") and not result:
        result.append({"kind": "link", "label": "Content unavailable through Meta's API",
                       "url": "", "preview_url": "", "unavailable": True})
    return result


def reply_window(last_customer_message: datetime | None, now: datetime) -> dict:
    """Conservative standard messaging window; never grants HUMAN_AGENT itself."""
    eligible = bool(last_customer_message and last_customer_message.tzinfo and
                    now.tzinfo and last_customer_message <= now)
    expires = last_customer_message + timedelta(hours=WINDOW_HOURS) if eligible else None
    can_reply = bool(expires and now < expires)
    if can_reply:
        reason = "Replies are available within 24 hours of the customer's latest message."
    elif expires:
        reason = "The 24-hour reply window has closed. Wait for a new customer message."
    else:
        reason = "A customer must message this account before you can reply."
    return {"can_reply": can_reply, "expires_at": expires.isoformat() if expires else "",
            "reason": reason, "human_agent_enabled": False, "policy_url": POLICY_URL}
