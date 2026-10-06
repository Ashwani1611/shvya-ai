"""First-party click tracking for WhatsApp template CTA buttons.

Quick replies continue to use contextual WhatsApp webhook receipts. Website,
Call, and Copy Code actions are represented to Meta as signed SHVYA URL
buttons. The public URL opens a confirmation page; only the confirmed action is
recorded, so link previews and security scanners do not inflate click counts.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core import signing

from apps.channels.models import WhatsAppTemplate

from . import template_analytics_base as analytics_base


TRACKING_SALT = "shvya.whatsapp.template.cta.v1"
TRACKING_PATH = "/w/cta/"
TRACKABLE_LOCAL_TYPES = {"visit_website", "call_phone", "copy_offer"}
ACTION_TO_BREAKDOWN_TYPE = {
    "url": "url_button",
    "call": "phone_button",
    "copy_code": "copy_code_button",
}
_VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*|\d+)\s*}}")
_INSTALLED = False


def _normalized_text(value):
    return " ".join(str(value or "").strip().split())


def _public_origin():
    origin = str(
        getattr(settings, "OPERATIONS_PUBLIC_ORIGIN", "")
        or "https://shvya-ai.com"
    ).strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return "https://shvya-ai.com"
    return origin


def _destination_tokens(value):
    return list(dict.fromkeys(_VARIABLE.findall(str(value or ""))))


def _definition(button):
    """Normalize one local button to a trackable action definition."""

    if not isinstance(button, dict):
        return None
    kind = str(button.get("type") or "").strip().lower()
    label = _normalized_text(button.get("text"))

    if kind == "visit_website":
        destination = str(button.get("url") or "").strip()
        tokens = _destination_tokens(destination)
        if len(tokens) > 1:
            from .template_service import TemplateError

            raise TemplateError(
                "A tracked website button can contain at most one URL parameter."
            )
        sample = _VARIABLE.sub("example", destination, count=1) if tokens else destination
        try:
            parsed = urlsplit(sample)
        except ValueError:
            parsed = None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            from .template_service import TemplateError

            raise TemplateError("Visit Website requires a valid public URL.")
        return {
            "action_type": "url",
            "destination": destination,
            "label": (label or "Visit website")[:80],
            "placeholder": tokens[0] if tokens else "",
        }

    if kind == "call_phone":
        destination = str(button.get("phone_number") or "").strip()
        if destination:
            return {
                "action_type": "call",
                "destination": destination,
                "label": (label or "Call")[:80],
                "placeholder": "",
            }

    if kind == "copy_offer":
        destination = str(button.get("coupon_code") or "").strip()
        if destination:
            return {
                "action_type": "copy_code",
                "destination": destination,
                "label": (label or "Copy code")[:80],
                "placeholder": "",
            }

    return None


def encode_cta_token(
    *,
    template,
    action_type,
    destination,
    label,
    button_index,
    card_index=None,
    placeholder="",
):
    return signing.dumps(
        {
            "v": 1,
            "t": str(template.pk),
            "a": str(action_type),
            "d": str(destination),
            "l": str(label)[:80],
            "b": int(button_index),
            "c": int(card_index) if card_index is not None else None,
            "p": str(placeholder or ""),
        },
        salt=TRACKING_SALT,
        compress=True,
    )


def decode_cta_token(token):
    data = signing.loads(str(token or ""), salt=TRACKING_SALT)
    if not isinstance(data, dict) or data.get("v") != 1:
        raise signing.BadSignature("Unsupported CTA token.")
    if str(data.get("a") or "") not in ACTION_TO_BREAKDOWN_TYPE:
        raise signing.BadSignature("Unsupported CTA action.")
    return data


def _tracking_url(*, template, definition, button_index, card_index=None):
    token = encode_cta_token(
        template=template,
        action_type=definition["action_type"],
        destination=definition["destination"],
        label=definition["label"],
        button_index=button_index,
        card_index=card_index,
        placeholder=definition.get("placeholder", ""),
    )
    token = quote(token, safe="._-~:")
    url = f"{_public_origin()}{TRACKING_PATH}{token}/"
    placeholder = definition.get("placeholder")
    if placeholder:
        url += "{{" + str(placeholder) + "}}/"
    if len(url) > 1900:
        from .template_service import TemplateError

        raise TemplateError("The tracked CTA URL is too long. Shorten the destination URL.")
    return url


def _tracked_meta_button(*, template, local_button, button_index, card_index=None):
    definition = _definition(local_button)
    if definition is None:
        return None
    url = _tracking_url(
        template=template,
        definition=definition,
        button_index=button_index,
        card_index=card_index,
    )
    payload = {
        "type": "URL",
        "text": definition["label"][:25],
        "url": url,
    }
    placeholder = definition.get("placeholder")
    if placeholder:
        payload["example"] = [
            url.replace("{{" + str(placeholder) + "}}", "example")
        ]
    return payload


def _buttons_component(components):
    return next(
        (
            component
            for component in components or []
            if isinstance(component, dict)
            and str(component.get("type") or "").upper() == "BUTTONS"
        ),
        None,
    )


def _transform_standard(*, template, components, base):
    component = _buttons_component(components)
    if component is None:
        return False
    remote_buttons = list(component.get("buttons") or [])
    changed = False
    for index, local_button in enumerate(base._ordered_buttons(template.buttons or [])):
        if index >= len(remote_buttons):
            break
        tracked = _tracked_meta_button(
            template=template,
            local_button=local_button,
            button_index=index,
        )
        if tracked is not None:
            remote_buttons[index] = tracked
            changed = True
    component["buttons"] = remote_buttons
    return changed


def _transform_carousel(*, template, components, state):
    carousel = next(
        (
            component
            for component in components or []
            if isinstance(component, dict)
            and str(component.get("type") or "").upper() == "CAROUSEL"
        ),
        None,
    )
    if carousel is None:
        return False

    config = state.carousel_config if isinstance(state.carousel_config, dict) else {}
    remote_cards = carousel.get("cards") or []
    changed = False
    for card_index, local_card in enumerate(config.get("cards") or []):
        if card_index >= len(remote_cards) or not isinstance(local_card, dict):
            break
        remote_card = remote_cards[card_index]
        if not isinstance(remote_card, dict):
            continue
        component = _buttons_component(remote_card.get("components") or [])
        if component is None:
            continue
        remote_buttons = list(component.get("buttons") or [])
        for button_index, local_button in enumerate(local_card.get("buttons") or []):
            if button_index >= len(remote_buttons):
                break
            tracked = _tracked_meta_button(
                template=template,
                local_button=local_button,
                button_index=button_index,
                card_index=card_index,
            )
            if tracked is not None:
                remote_buttons[button_index] = tracked
                changed = True
        component["buttons"] = remote_buttons
    return changed


def apply_tracking_to_meta_payload(*, template, payload, base):
    """Replace native non-reply CTAs with signed SHVYA action URLs."""

    components = copy.deepcopy(payload.get("components") or [])
    state = base.state_for(template)
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        changed = _transform_carousel(
            template=template,
            components=components,
            state=state,
        )
    else:
        changed = _transform_standard(
            template=template,
            components=components,
            base=base,
        )
    if not changed:
        return payload
    payload["components"] = components
    state.components = components
    state.save(update_fields=["components", "updated_at"])
    return payload


def _walk_buttons(components):
    for component in components or []:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").upper()
        if kind == "BUTTONS":
            yield from (component.get("buttons") or [])
        elif kind == "CAROUSEL":
            for card in component.get("cards") or []:
                if isinstance(card, dict):
                    yield from _walk_buttons(card.get("components") or [])


def is_tracking_url(value):
    try:
        return TRACKING_PATH in (urlsplit(str(value or "")).path or "")
    except ValueError:
        return False


def components_have_tracking(components):
    return any(
        isinstance(button, dict)
        and str(button.get("type") or "").upper() == "URL"
        and is_tracking_url(button.get("url"))
        for button in _walk_buttons(components)
    )


def _decode_tracking_button(button):
    if not isinstance(button, dict) or not is_tracking_url(button.get("url")):
        return None
    try:
        path = urlsplit(str(button.get("url") or "")).path
        token = path.split(TRACKING_PATH, 1)[1].split("/", 1)[0]
        data = decode_cta_token(token)
    except (IndexError, ValueError, signing.BadSignature):
        return None

    action_type = data["a"]
    if action_type == "url":
        return {
            "type": "visit_website",
            "text": data.get("l") or button.get("text") or "Visit website",
            "url": data.get("d") or "",
            "phone_number": "",
        }
    if action_type == "call":
        return {
            "type": "call_phone",
            "text": data.get("l") or button.get("text") or "Call",
            "url": "",
            "phone_number": data.get("d") or "",
        }
    return {
        "type": "copy_offer",
        "text": data.get("l") or button.get("text") or "Copy code",
        "url": "",
        "phone_number": "",
        "coupon_code": data.get("d") or "",
    }


def _native_button(button):
    if not isinstance(button, dict):
        return None
    kind = str(button.get("type") or "").upper()
    if kind == "URL":
        return {
            "type": "visit_website",
            "text": button.get("text") or "Visit website",
            "url": button.get("url") or "",
            "phone_number": "",
        }
    if kind == "PHONE_NUMBER":
        return {
            "type": "call_phone",
            "text": button.get("text") or "Call",
            "url": "",
            "phone_number": button.get("phone_number") or "",
        }
    if kind == "COPY_CODE":
        example = button.get("example")
        if isinstance(example, list):
            example = example[0] if example else ""
        return {
            "type": "copy_offer",
            "text": button.get("text") or "Copy code",
            "url": "",
            "phone_number": "",
            "coupon_code": example or button.get("coupon_code") or "",
        }
    if kind == "QUICK_REPLY":
        return {
            "type": "text_back",
            "text": button.get("text") or "Quick reply",
            "url": "",
            "phone_number": "",
        }
    return None


def source_buttons_for_template(template):
    """Return editable local buttons, including remote-only templates."""

    if template.buttons:
        return copy.deepcopy(template.buttons)
    try:
        state = template.meta_state
    except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
        return []
    component = _buttons_component(state.components)
    if component is None:
        return []
    buttons = []
    for remote in component.get("buttons") or []:
        parsed = _decode_tracking_button(remote) or _native_button(remote)
        if parsed is not None:
            buttons.append(parsed)
    return buttons


def template_has_trackable_cta(template):
    if any(
        isinstance(button, dict)
        and str(button.get("type") or "") in TRACKABLE_LOCAL_TYPES
        for button in source_buttons_for_template(template)
    ):
        return True
    try:
        config = template.meta_state.carousel_config
    except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
        config = {}
    return any(
        isinstance(button, dict)
        and str(button.get("type") or "") in TRACKABLE_LOCAL_TYPES
        for card in (config or {}).get("cards") or []
        if isinstance(card, dict)
        for button in card.get("buttons") or []
    )


def template_tracking_enabled(template):
    try:
        return components_have_tracking(template.meta_state.components)
    except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
        return False


def _breakdown(mapping):
    rows = [
        {
            "type": key[0],
            "button_content": key[1],
            "count": count,
        }
        for key, count in mapping.items()
        if count > 0
    ]
    rows.sort(
        key=lambda row: (-row["count"], row["button_content"], row["type"])
    )
    return rows


def augment_tracked_cta_events(
    *,
    account,
    template_ids,
    start_date,
    end_date,
    local_results,
):
    """Add confirmed Website, Call and Copy Code actions to analytics."""

    ids = list(dict.fromkeys(str(value) for value in template_ids if value))
    if not ids or not local_results:
        return local_results

    templates = list(
        WhatsAppTemplate.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=ids,
        ).select_related("meta_state")
    )
    local_to_meta = {
        str(template.pk): str(template.meta_template_id)
        for template in templates
        if template.meta_template_id
    }
    tracked_ids = {
        str(template.meta_template_id)
        for template in templates
        if template.meta_template_id and template_tracking_enabled(template)
    }
    if not tracked_ids:
        return local_results

    day_maps = {
        template_id: {
            str(row.get("date")): row
            for row in (result.get("days") or [])
            if row.get("date")
        }
        for template_id, result in local_results.items()
    }
    click_counts = {template_id: defaultdict(int) for template_id in ids}
    existing_unique = {template_id: defaultdict(int) for template_id in ids}
    event_unique = {template_id: defaultdict(set) for template_id in ids}

    for template_id, result in local_results.items():
        for row in result.get("clicks") or []:
            key = (
                str(row.get("type") or "button"),
                _normalized_text(row.get("button_content")),
            )
            click_counts[template_id][key] += analytics_base._as_int(row.get("count"))
        for row in result.get("unique_clicks") or []:
            key = (
                str(row.get("type") or "button"),
                _normalized_text(row.get("button_content")),
            )
            existing_unique[template_id][key] += analytics_base._as_int(
                row.get("count")
            )

    start_at = datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc)
    end_at = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=dt_timezone.utc,
    )
    from apps.channels.template_cta_models import WhatsAppTemplateCTAEvent

    events = WhatsAppTemplateCTAEvent.objects.filter(
        organization_id=account.organization_id,
        account_id=account.pk,
        template_id__in=set(local_to_meta),
        clicked_at__gte=start_at,
        clicked_at__lt=end_at,
    ).values(
        "template_id",
        "action_type",
        "button_label",
        "visitor_hash",
        "clicked_at",
    )
    applied = {template_id: 0 for template_id in ids}
    for event in events.iterator(chunk_size=1000):
        template_id = local_to_meta.get(str(event["template_id"]))
        if template_id not in local_results:
            continue
        day = event["clicked_at"].astimezone(dt_timezone.utc).date().isoformat()
        day_row = day_maps.get(template_id, {}).get(day)
        if day_row is None:
            continue
        day_row["clicked"] = analytics_base._as_int(day_row.get("clicked")) + 1
        key = (
            ACTION_TO_BREAKDOWN_TYPE.get(event["action_type"], "button"),
            _normalized_text(event["button_label"]) or "Button",
        )
        click_counts[template_id][key] += 1
        event_unique[template_id][key].add(event["visitor_hash"])
        applied[template_id] += 1

    for template_id in tracked_ids:
        result = local_results.get(template_id)
        if result is None:
            continue
        availability = result.setdefault("availability", {})
        availability["clicked"] = True
        availability["unique_clicked"] = True
        result["clicks"] = _breakdown(click_counts[template_id])

        unique_counts = defaultdict(int, existing_unique[template_id])
        for key, visitors in event_unique[template_id].items():
            unique_counts[key] += len(visitors)
        result["unique_clicks"] = _breakdown(unique_counts)

        clicked_total = sum(
            analytics_base._as_int(row.get("clicked"))
            for row in result.get("days") or []
        )
        unique_total = sum(
            analytics_base._as_int(row.get("count"))
            for row in result["unique_clicks"]
        )
        result.setdefault("totals", {})["clicked"] = clicked_total
        result.setdefault("rates", {})["clicked"] = analytics_base._rate(
            clicked_total,
            result.get("totals", {}).get("delivered", 0),
        )
        result["unique_click_total"] = unique_total
        result["unique_click_rate"] = analytics_base._rate(
            unique_total,
            result.get("totals", {}).get("delivered", 0),
        )
        result["click_count_basis"] = (
            "local_all_buttons"
            if result.get("click_count_basis") == "local_quick_reply"
            else "shvya_tracked_cta"
        )
        result["click_source"] = "shvya_cta_receipts"
        result["click_source_label"] = "SHVYA confirmed CTA receipts"
        result["click_scope"] = "all_tracked_buttons"
        result["click_data_partial"] = False
        result["local_click_receipts_available"] = True
        result["local_click_receipts_applied"] = bool(
            result.get("local_click_receipts_applied") or applied[template_id]
        )
        if result.get("source") == "shvya":
            result["source_label"] = "SHVYA delivery and CTA receipts"

    return local_results


def install_template_cta_tracking():
    """Install payload transformation before template transports import it."""

    global _INSTALLED
    if _INSTALLED:
        return

    from . import template_service as base

    original_build_meta_payload = base.build_meta_payload
    original_validate_button_set = base._validate_button_set

    def validate_button_set(buttons):
        counts = original_validate_button_set(buttons)
        tracked_count = sum(
            counts.get(kind, 0)
            for kind in ("visit_website", "call_phone", "copy_offer")
        )
        if tracked_count > 2:
            raise base.TemplateError(
                "SHVYA click tracking supports at most two Website, Call, or Copy Code buttons in one standard template."
            )
        return counts

    def build_meta_payload(*, template, header_handle="", carousel_config=None):
        payload = original_build_meta_payload(
            template=template,
            header_handle=header_handle,
            carousel_config=carousel_config,
        )
        return apply_tracking_to_meta_payload(
            template=template,
            payload=payload,
            base=base,
        )

    base._validate_button_set = validate_button_set
    base.build_meta_payload = build_meta_payload
    WhatsAppTemplate.has_trackable_cta = property(template_has_trackable_cta)
    WhatsAppTemplate.cta_tracking_enabled = property(template_tracking_enabled)
    _INSTALLED = True
