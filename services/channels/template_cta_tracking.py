"""Independent SHVYA click tracking for WhatsApp template CTAs.

Quick replies remain webhook-tracked. Marketing and utility Website, Call, and
Copy Code actions are registered with Meta as dynamic URL buttons pointing to a
per-send SHVYA token. Website actions redirect immediately; Call and Copy Code
open a small action page. This makes the displayed click total an observed
SHVYA event even when Meta omits the optional ``clicked`` analytics property.
"""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from apps.channels.models import WhatsAppTemplate
from apps.channels.tracking_models import (
    WhatsAppTemplateTrackedClick,
    WhatsAppTemplateTrackedLink,
)

logger = logging.getLogger(__name__)

TRACK_MARKER = "_shvya_track_cta"
TRACKABLE_LOCAL_TYPES = {"visit_website", "call_phone", "copy_offer"}
TRACKING_ROUTE_PATH = "/w/cta/"
TRACKING_TOKEN = "{{1}}"
_INSTALLED = False


def tracking_origin():
    origin = str(
        getattr(settings, "CTA_TRACKING_PUBLIC_BASE_URL", "")
        or getattr(settings, "OPERATIONS_PUBLIC_BASE_URL", "")
        or "https://dashboard.shvya-ai.com"
    ).strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise RuntimeError("CTA tracking public origin is invalid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("CTA tracking public origin must be an absolute HTTP(S) URL.")
    if parsed.scheme != "https" and not settings.DEBUG:
        raise RuntimeError("CTA tracking public origin must use HTTPS outside development.")
    return origin


def tracking_url_template():
    return f"{tracking_origin()}{TRACKING_ROUTE_PATH}{TRACKING_TOKEN}"


def tracking_example_url():
    return tracking_url_template().replace(
        TRACKING_TOKEN,
        "00000000-0000-4000-8000-000000000001",
    )


def is_tracking_url(value):
    raw = str(value or "").strip().replace("%7B%7B1%7D%7D", TRACKING_TOKEN)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path == f"{TRACKING_ROUTE_PATH}{TRACKING_TOKEN}"
    )


def _tracked_meta_button(item):
    kind = str(item.get("type") or "").strip()
    text = str(item.get("text") or "").strip()
    if kind == "copy_offer":
        text = text or "Copy code"
    elif kind == "call_phone":
        text = text or "Call"
    else:
        text = text or "Open link"
    return {
        "type": "URL",
        "text": text[:25],
        "url": tracking_url_template(),
        "example": [tracking_example_url()],
    }


def _tracking_action(item):
    kind = str(item.get("type") or "").strip()
    if kind == "visit_website":
        return {
            "action_type": WhatsAppTemplateTrackedLink.ActionType.WEBSITE,
            "button_text": str(item.get("text") or "Visit website").strip()[:80],
            "destination_url": str(item.get("url") or "").strip(),
            "phone_number": "",
            "coupon_code": "",
        }
    if kind == "call_phone":
        return {
            "action_type": WhatsAppTemplateTrackedLink.ActionType.CALL,
            "button_text": str(item.get("text") or "Call").strip()[:80],
            "destination_url": "",
            "phone_number": str(item.get("phone_number") or "").strip()[:32],
            "coupon_code": "",
        }
    if kind == "copy_offer":
        return {
            "action_type": WhatsAppTemplateTrackedLink.ActionType.COPY_CODE,
            "button_text": str(item.get("text") or "Copy code").strip()[:80],
            "destination_url": "",
            "phone_number": "",
            "coupon_code": str(item.get("coupon_code") or "").strip()[:128],
        }
    return None


def _buttons_component(components):
    for component in components or []:
        if (
            isinstance(component, dict)
            and str(component.get("type") or "").strip().upper() == "BUTTONS"
        ):
            return component
    return {}


def _carousel_component(components):
    for component in components or []:
        if (
            isinstance(component, dict)
            and str(component.get("type") or "").strip().upper() == "CAROUSEL"
        ):
            return component
    return {}


def _tracking_specs(template):
    if template.category not in {
        WhatsAppTemplate.Category.MARKETING,
        WhatsAppTemplate.Category.UTILITY,
    }:
        return []
    try:
        state = template.meta_state
    except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
        return []
    components = state.components if isinstance(state.components, list) else []
    specs = []

    if template.template_format == WhatsAppTemplate.Format.STANDARD:
        from . import template_service

        local_buttons = template_service._ordered_buttons(template.buttons or [])
        meta_buttons = _buttons_component(components).get("buttons") or []
        for index, local_button in enumerate(local_buttons):
            if index >= len(meta_buttons):
                continue
            meta_button = meta_buttons[index]
            if not isinstance(meta_button, dict) or not is_tracking_url(meta_button.get("url")):
                continue
            action = _tracking_action(local_button)
            if not action:
                continue
            specs.append(
                {
                    **action,
                    "button_path": f"button{index}",
                    "button_index": index,
                    "card_index": None,
                }
            )
        return specs

    config = state.carousel_config if isinstance(state.carousel_config, dict) else {}
    meta_cards = _carousel_component(components).get("cards") or []
    for card_index, local_card in enumerate(config.get("cards") or []):
        if not isinstance(local_card, dict) or card_index >= len(meta_cards):
            continue
        meta_card = meta_cards[card_index]
        meta_buttons = _buttons_component(
            meta_card.get("components") if isinstance(meta_card, dict) else []
        ).get("buttons") or []
        for button_index, local_button in enumerate(local_card.get("buttons") or []):
            if button_index >= len(meta_buttons):
                continue
            meta_button = meta_buttons[button_index]
            if not isinstance(meta_button, dict) or not is_tracking_url(meta_button.get("url")):
                continue
            action = _tracking_action(local_button)
            if not action:
                continue
            specs.append(
                {
                    **action,
                    "button_path": f"card{card_index}.button{button_index}",
                    "button_index": button_index,
                    "card_index": card_index,
                }
            )
    return specs


def _replace_button_component(parts, *, index, token):
    replacement = {
        "type": "button",
        "sub_type": "url",
        "index": str(index),
        "parameters": [{"type": "text", "text": str(token)}],
    }
    filtered = []
    for part in parts or []:
        if not isinstance(part, dict):
            filtered.append(part)
            continue
        is_same = (
            str(part.get("type") or "").lower() == "button"
            and str(part.get("sub_type") or "").lower() == "url"
            and str(part.get("index")) == str(index)
        )
        if not is_same:
            filtered.append(part)
    filtered.append(replacement)
    return filtered


def prepare_message_tracking(*, message, template, components):
    """Create/reuse per-send links and inject their URL suffix parameters."""

    specs = _tracking_specs(template)
    if not specs:
        return components, []

    rendered = copy.deepcopy(components if isinstance(components, list) else [])
    links = []
    for spec in specs:
        defaults = {
            "organization": message.organization,
            "account": message.account,
            "template": template,
            "lead": message.lead,
            "meta_template_id": str(template.meta_template_id or ""),
            "template_name": str(template.name or "")[:150],
            "button_index": spec["button_index"],
            "card_index": spec["card_index"],
            "action_type": spec["action_type"],
            "button_text": spec["button_text"],
            "destination_url": spec["destination_url"],
            "phone_number": spec["phone_number"],
            "coupon_code": spec["coupon_code"],
            "is_active": True,
        }
        link, created = WhatsAppTemplateTrackedLink.objects.get_or_create(
            message=message,
            button_path=spec["button_path"],
            defaults=defaults,
        )
        if not created:
            changed = []
            for field, value in defaults.items():
                field_name = f"{field}_id" if field in {"organization", "account", "template", "lead"} else field
                target_value = value.pk if hasattr(value, "pk") else value
                if getattr(link, field_name) != target_value:
                    setattr(link, field, value)
                    changed.append(field)
            if changed:
                changed.append("updated_at")
                link.save(update_fields=changed)
        links.append(link)

        if spec["card_index"] is None:
            rendered = _replace_button_component(
                rendered,
                index=spec["button_index"],
                token=link.token,
            )
            continue

        carousel = next(
            (
                part
                for part in rendered
                if isinstance(part, dict)
                and str(part.get("type") or "").lower() == "carousel"
            ),
            None,
        )
        if carousel is None:
            carousel = {"type": "carousel", "cards": []}
            rendered.append(carousel)
        cards = carousel.setdefault("cards", [])
        card = next(
            (
                candidate
                for candidate in cards
                if str(candidate.get("card_index")) == str(spec["card_index"])
            ),
            None,
        )
        if card is None:
            card = {"card_index": spec["card_index"], "components": []}
            cards.append(card)
        card["components"] = _replace_button_component(
            card.get("components") or [],
            index=spec["button_index"],
            token=link.token,
        )

    return rendered, links


def _tracking_field_keys(spec):
    keys = set()

    def walk(components, prefix=""):
        for component in components or []:
            if not isinstance(component, dict):
                continue
            kind = str(component.get("type") or "").lower()
            if kind == "carousel":
                for index, card in enumerate(component.get("cards") or []):
                    walk(card.get("components") or [], f"card{index}.")
            elif kind == "buttons":
                for index, button in enumerate(component.get("buttons") or []):
                    if not isinstance(button, dict) or not is_tracking_url(button.get("url")):
                        continue
                    keys.add(f"{prefix}button{index}.1")

    walk((spec or {}).get("components") or [])
    return keys


def _without_tracking_tokens(spec):
    clean = copy.deepcopy(spec)

    def walk(components):
        for component in components or []:
            if not isinstance(component, dict):
                continue
            kind = str(component.get("type") or "").lower()
            if kind == "carousel":
                for card in component.get("cards") or []:
                    walk(card.get("components") or [])
            elif kind == "buttons":
                for button in component.get("buttons") or []:
                    if isinstance(button, dict) and is_tracking_url(button.get("url")):
                        button["url"] = str(button.get("url") or "").replace(
                            TRACKING_TOKEN,
                            "",
                        )

    walk(clean.get("components") or [])
    return clean


def _merge_breakdowns(existing_rows, local_rows):
    merged = {}
    for rows in (existing_rows or [], local_rows or []):
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "button").strip().lower()
            if kind.startswith("unique_"):
                kind = kind.removeprefix("unique_")
            content = " ".join(str(row.get("button_content") or "").split())
            key = (kind, content.casefold())
            count = max(0, int(row.get("count") or 0))
            current = merged.get(key)
            if current is None or count > current["count"]:
                merged[key] = {
                    "type": kind,
                    "button_content": content,
                    "count": count,
                }
    return sorted(
        merged.values(),
        key=lambda row: (-row["count"], row["button_content"], row["type"]),
    )


def _tracked_templates(account, template_ids):
    rows = WhatsAppTemplate.objects.filter(
        organization_id=account.organization_id,
        account_id=account.pk,
        meta_template_id__in=template_ids,
    ).select_related("meta_state")
    result = set()
    for template in rows:
        try:
            components = template.meta_state.components
        except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
            components = []

        def contains(items):
            for component in items or []:
                if not isinstance(component, dict):
                    continue
                kind = str(component.get("type") or "").upper()
                if kind == "BUTTONS" and any(
                    isinstance(button, dict) and is_tracking_url(button.get("url"))
                    for button in component.get("buttons") or []
                ):
                    return True
                if kind == "CAROUSEL":
                    for card in component.get("cards") or []:
                        if contains(card.get("components") if isinstance(card, dict) else []):
                            return True
            return False

        if contains(components):
            result.add(str(template.meta_template_id))
    return result


def augment_tracked_cta_analytics(
    *,
    account,
    template_ids,
    start_date,
    end_date,
    results,
):
    """Merge observed tracked CTA requests into normalized analytics."""

    ids = list(dict.fromkeys(str(value) for value in template_ids if value))
    if not ids or not results:
        return results

    tracked_ids = _tracked_templates(account, ids)
    for meta_id in tracked_ids:
        result = results.get(meta_id)
        if not result:
            continue
        availability = result.setdefault("availability", {})
        availability["clicked"] = True
        availability["unique_clicked"] = True
        result.setdefault("click_count_basis", "shvya_tracked_cta")
        result.setdefault("click_source", "shvya_tracked_cta")
        result.setdefault("click_source_label", "SHVYA tracked CTA receipts")

    start_at = datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc)
    end_at = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=dt_timezone.utc,
    )
    links = (
        WhatsAppTemplateTrackedLink.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=ids,
            is_active=True,
            sent_at__gte=start_at,
            sent_at__lt=end_at,
        )
        .annotate(
            click_total=Count(
                "events",
                filter=Q(
                    events__event_type=WhatsAppTemplateTrackedClick.EventType.CLICK
                ),
            )
        )
        .order_by()
    )

    total_maps = {meta_id: defaultdict(int) for meta_id in ids}
    unique_maps = {meta_id: defaultdict(int) for meta_id in ids}
    type_map = {
        WhatsAppTemplateTrackedLink.ActionType.WEBSITE: "url_button",
        WhatsAppTemplateTrackedLink.ActionType.CALL: "call_button",
        WhatsAppTemplateTrackedLink.ActionType.COPY_CODE: "copy_code_button",
    }
    for link in links:
        meta_id = str(link.meta_template_id or "")
        if meta_id not in results:
            continue
        kind = type_map.get(link.action_type, "button")
        key = (kind, link.button_text or kind.replace("_", " ").title())
        count = int(link.click_total or 0)
        total_maps[meta_id][key] += count
        if count > 0:
            unique_maps[meta_id][key] += 1

    for meta_id, result in results.items():
        if meta_id not in tracked_ids:
            continue
        local_clicks = [
            {"type": key[0], "button_content": key[1], "count": count}
            for key, count in total_maps[meta_id].items()
            if count > 0
        ]
        local_unique = [
            {"type": key[0], "button_content": key[1], "count": count}
            for key, count in unique_maps[meta_id].items()
            if count > 0
        ]
        result["clicks"] = _merge_breakdowns(result.get("clicks"), local_clicks)
        result["unique_clicks"] = _merge_breakdowns(
            result.get("unique_clicks"),
            local_unique,
        )
        breakdown_total = sum(int(row.get("count") or 0) for row in result["clicks"])
        existing_total = int((result.get("totals") or {}).get("clicked") or 0)
        clicked_total = max(existing_total, breakdown_total)
        result.setdefault("totals", {})["clicked"] = clicked_total
        delivered = int(result["totals"].get("delivered") or 0)
        result.setdefault("rates", {})["clicked"] = (
            round((clicked_total / delivered) * 100, 1) if delivered else None
        )
        unique_total = sum(
            int(row.get("count") or 0) for row in result["unique_clicks"]
        )
        result["unique_click_total"] = unique_total
        result["unique_click_rate"] = (
            round((unique_total / delivered) * 100, 1) if delivered else None
        )
        if local_clicks or result.get("source") == "shvya":
            result["source"] = (
                "meta+shvya" if str(result.get("source") or "").startswith("meta") else "shvya"
            )
            result["source_label"] = (
                "Meta insights + SHVYA tracked CTA receipts"
                if result["source"] == "meta+shvya"
                else "SHVYA delivery and tracked CTA receipts"
            )
            result["click_source"] = "shvya_tracked_cta"
            result["click_source_label"] = "SHVYA tracked CTA receipts"
            result["click_count_basis"] = "observed_tracked_cta"
            result["click_scope"] = "tracked_cta"
            result["click_data_partial"] = False

    return results


def _enable_meta_click_tracking(template):
    if (
        not template.meta_template_id
        or template.category
        not in {
            WhatsAppTemplate.Category.MARKETING,
            WhatsAppTemplate.Category.UTILITY,
        }
    ):
        return False
    cache_key = f"wa:template-click-tracking:{template.account_id}:{template.meta_template_id}"
    if not cache.add(cache_key, "attempted", timeout=24 * 60 * 60):
        return False
    from apps.channels.providers import whatsapp as meta

    try:
        response = meta.requests.post(
            f"{meta.GRAPH_API_BASE}/{template.meta_template_id}",
            headers={"Authorization": f"Bearer {template.account.access_token}"},
            json={
                "cta_url_link_tracking_opted_out": False,
                "category": str(template.category).upper(),
            },
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        logger.warning("Could not enable Meta template click tracking for %s: %s", template.pk, exc)
        return False
    if not response.ok:
        logger.warning(
            "Meta rejected template click tracking enablement for %s: HTTP %s %s",
            template.pk,
            response.status_code,
            str(response.text or "")[:300],
        )
        return False
    return True


def install_template_cta_tracking():
    """Install focused compatibility wrappers without duplicating send paths."""

    global _INSTALLED
    if _INSTALLED:
        return

    from . import campaign_policy
    from . import template_analytics
    from . import template_meta_fix
    from . import template_rendering
    from . import template_service
    from . import whatsapp_template_delivery

    original_validate_buttons = template_service._validate_button_set
    original_button = template_service._button
    original_carousel_button = template_service._carousel_button_payload
    original_build_meta_payload = template_service.build_meta_payload
    original_template_fields = campaign_policy.template_fields
    original_render_message = campaign_policy.render_message
    original_send_transport = whatsapp_template_delivery._send_template_transport
    original_fetch_analytics = template_analytics.fetch_template_analytics
    original_submit_template = template_meta_fix.submit_template
    original_sync_templates = template_meta_fix.sync_templates

    def validate_buttons(buttons):
        counts = original_validate_buttons(buttons)
        tracked_count = sum(
            1
            for button in buttons or []
            if isinstance(button, dict)
            and str(button.get("type") or "") in TRACKABLE_LOCAL_TYPES
        )
        if tracked_count > 2:
            raise template_service.TemplateError(
                "A tracked template can contain at most two Website, Call, or Copy Code CTAs. "
                "Use Quick Reply buttons for additional actions."
            )
        return counts

    def button(item):
        if isinstance(item, dict) and item.get(TRACK_MARKER):
            return _tracked_meta_button(item)
        return original_button(item)

    def carousel_button(item):
        if isinstance(item, dict) and item.get(TRACK_MARKER):
            return _tracked_meta_button(item)
        return original_carousel_button(item)

    def build_meta_payload(*, template, header_handle="", carousel_config=None):
        if template.category not in {
            WhatsAppTemplate.Category.MARKETING,
            WhatsAppTemplate.Category.UTILITY,
        }:
            return original_build_meta_payload(
                template=template,
                header_handle=header_handle,
                carousel_config=carousel_config,
            )
        if template.template_format == WhatsAppTemplate.Format.STANDARD:
            original = template.buttons
            template.buttons = [
                {
                    **item,
                    TRACK_MARKER: str(item.get("type") or "")
                    in TRACKABLE_LOCAL_TYPES,
                }
                if isinstance(item, dict)
                else item
                for item in original or []
            ]
            try:
                return original_build_meta_payload(
                    template=template,
                    header_handle=header_handle,
                    carousel_config=carousel_config,
                )
            finally:
                template.buttons = original

        config = copy.deepcopy(
            carousel_config
            if isinstance(carousel_config, dict)
            else template_service.state_for(template).carousel_config
        )
        for card in config.get("cards") or []:
            if not isinstance(card, dict):
                continue
            card["buttons"] = [
                {
                    **item,
                    TRACK_MARKER: str(item.get("type") or "")
                    in {"visit_website", "call_phone"},
                }
                if isinstance(item, dict)
                else item
                for item in card.get("buttons") or []
            ]
        return original_build_meta_payload(
            template=template,
            header_handle=header_handle,
            carousel_config=config,
        )

    def template_fields(spec):
        hidden = _tracking_field_keys(spec)
        return [field for field in original_template_fields(spec) if field["key"] not in hidden]

    def render_message(spec, bindings, values, allowed_sources):
        return original_render_message(
            _without_tracking_tokens(spec),
            bindings,
            values,
            allowed_sources,
        )

    def send_transport(message):
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        template = WhatsAppTemplate.objects.filter(
            pk=payload.get("template_id"),
            organization_id=message.organization_id,
            account_id=message.account_id,
        ).first()
        links = []
        if template is not None:
            components, links = prepare_message_tracking(
                message=message,
                template=template,
                components=payload.get("components") or [],
            )
            if links:
                updated = dict(payload)
                updated["components"] = components
                updated["tracked_cta_link_ids"] = [str(link.id) for link in links]
                message.media_payload = updated
                message.save(update_fields=["media_payload", "updated_at"])
        result = original_send_transport(message)
        if links:
            sent_at = result.sent_at or timezone.now()
            WhatsAppTemplateTrackedLink.objects.filter(
                id__in=[link.id for link in links]
            ).update(is_active=True, sent_at=sent_at, updated_at=timezone.now())
        return result

    def fetch_analytics(*, account, template_ids, start_date, end_date):
        result = original_fetch_analytics(
            account=account,
            template_ids=template_ids,
            start_date=start_date,
            end_date=end_date,
        )
        templates = WhatsAppTemplate.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=template_ids,
            status=WhatsAppTemplate.Status.APPROVED,
        ).select_related("account")
        for template in templates:
            _enable_meta_click_tracking(template)
        return augment_tracked_cta_analytics(
            account=account,
            template_ids=template_ids,
            start_date=start_date,
            end_date=end_date,
            results=result,
        )

    def submit_template(*, template, attachment_file=None, carousel_files=None):
        result = original_submit_template(
            template=template,
            attachment_file=attachment_file,
            carousel_files=carousel_files,
        )
        _enable_meta_click_tracking(result)
        return result

    def sync_templates(*, organization, account):
        summary = original_sync_templates(organization=organization, account=account)
        for template in WhatsAppTemplate.objects.filter(
            organization=organization,
            account=account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).exclude(meta_template_id=""):
            _enable_meta_click_tracking(template)
        return summary

    template_service._validate_button_set = validate_buttons
    template_service._button = button
    template_service._carousel_button_payload = carousel_button
    template_service.build_meta_payload = build_meta_payload
    template_rendering._button = button
    campaign_policy.template_fields = template_fields
    template_rendering.template_fields = template_fields
    campaign_policy.render_message = render_message
    template_rendering.render_message = render_message
    whatsapp_template_delivery._send_template_transport = send_transport
    template_analytics.fetch_template_analytics = fetch_analytics
    template_meta_fix.submit_template = submit_template
    template_meta_fix.sync_templates = sync_templates

    _INSTALLED = True
