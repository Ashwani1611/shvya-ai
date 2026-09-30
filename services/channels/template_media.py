"""Reusable template files. Approval handles are never message-time media IDs."""
from copy import deepcopy
import logging
from uuid import uuid4

from django.core.files.storage import default_storage
from django.core.cache import cache
from django.db import transaction

from .campaign_policy import CampaignInputError, _groups, media_parameter

logger = logging.getLogger(__name__)


def remap_carousel_delivery_media(stored, old_config, new_config):
    """Keep uploaded files with their card identity when a draft is reordered."""
    old_cards = {
        card.get("uid"): (index, card)
        for index, card in enumerate((old_config or {}).get("cards", []))
        if isinstance(card, dict) and card.get("uid")
    }
    result = {}
    for index, card in enumerate((new_config or {}).get("cards", [])):
        previous = old_cards.get(card.get("uid"))
        if not previous:
            continue
        old_index, old_card = previous
        asset = (stored or {}).get(f"card{old_index}.header.media", {})
        if (
            old_card.get("media_type") == card.get("media_type")
            and asset.get("kind") == card.get("media_type")
            and asset.get("asset")
        ):
            result[f"card{index}.header.media"] = deepcopy(asset)
    return result


def save_delivery_media(*, template, field, kind, uploaded_file):
    from apps.channels.template_models import WhatsAppTemplateMetadata
    from .template_service import _validate_media_file, state_for

    mime, _ = _validate_media_file(kind, uploaded_file)
    asset = uuid4().hex
    uploaded_file.seek(0)
    path = default_storage.save(
        f"template-media/{template.organization_id}/{template.account_id}/{template.pk}/{asset}",
        uploaded_file,
    )
    entry = {"asset": asset, "path": path, "kind": kind, "mime": mime,
             "name": str(getattr(uploaded_file, "name", "attachment"))[:255]}
    try:
        with transaction.atomic():
            state_for(template)
            state = WhatsAppTemplateMetadata.objects.select_for_update().get(template=template)
            media = dict(state.delivery_media or {})
            media[field] = entry
            state.delivery_media = media
            state.save(update_fields=["delivery_media", "updated_at"])
    except Exception:
        # Only the newly uploaded, unreferenced blob is eligible for cleanup.
        # Prior files can still be referenced by reviewed campaign snapshots.
        try:
            default_storage.delete(path)
        except Exception:
            logger.warning("Could not remove an unreferenced template media upload for template %s", template.pk)
        raise
    return entry


def media_defaults(state, components):
    result = {}
    stored = state.delivery_media or {}
    for prefix, kind, component in _groups(components):
        fmt = str(component.get("format", "TEXT")).lower()
        if kind != "header" or fmt not in {"image", "video", "document"}:
            continue
        key = f"{prefix}header.media"
        asset = stored.get(key, {})
        if asset.get("kind") == fmt and asset.get("asset"):
            result[key] = f"asset:{asset['asset']}"
            continue
        # Synced templates can expose an HTTPS sample, but opaque resumable
        # upload handles must never be treated as a URL or /media ID.
        handles = (component.get("example") or {}).get("header_handle") or []
        if isinstance(handles, list) and handles:
            try:
                value = str(handles[0])
                if value.startswith("https://"):
                    media_parameter(value, fmt)
                    result[key] = value
            except CampaignInputError:
                pass
    return result


def resolve_delivery_media(*, components, template, client):
    """Resolve private assets in the worker using the template's own account."""
    from .template_service import state_for

    output = deepcopy(components)
    stored = state_for(template).delivery_media or {}
    uploaded = {}

    def visit(parts, prefix=""):
        for component in parts:
            if component.get("type") == "carousel":
                for card in component.get("cards", []):
                    visit(card.get("components", []), f"card{card['card_index']}.")
            for parameter in component.get("parameters", []):
                kind = parameter.get("type")
                if kind not in {"image", "video", "document"}:
                    continue
                media = parameter.get(kind, {})
                asset_id = media.get("shvya_asset")
                if not asset_id:
                    continue
                asset = stored.get(f"{prefix}header.media", {})
                if asset.get("asset") != asset_id or asset.get("kind") != kind:
                    raise CampaignInputError("Template attachment changed. Review the template and campaign again.")
                if asset_id not in uploaded:
                    key = f"template-media:{template.organization_id}:{template.account_id}:{asset_id}"
                    media_id = cache.get(key)
                    if not media_id:
                        with default_storage.open(asset["path"], "rb") as file_obj:
                            result = client.upload_media(file_obj=file_obj, filename=asset["name"], mime_type=asset["mime"])
                        media_id = str(result["id"])
                        cache.set(key, media_id, timeout=3600)
                    uploaded[asset_id] = media_id
                parameter[kind] = {"id": uploaded[asset_id]}
    visit(output)
    return output
