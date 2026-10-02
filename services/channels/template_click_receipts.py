"""Local WhatsApp template button-click receipts.

Meta template analytics is the canonical source for URL and quick-reply click
metrics, but the provider can omit the ``clicked`` field for an otherwise valid
analytics response. Quick-reply taps are also delivered to SHVYA as inbound
messages containing the contextual WhatsApp message ID of the outbound
template. This module links those durable inbound receipts back to the exact
outbound template send and uses them as a non-duplicating floor/fallback.

Only quick replies are inferred locally. URL, phone, and copy-code button taps
do not produce a reliable inbound message receipt and are never fabricated.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.db.models import Q
from django.utils import timezone

from apps.channels.models import WhatsAppMessage, WhatsAppTemplate

from . import template_analytics_base as _base


CLICK_ATTRIBUTION_DAYS = 7
QUICK_REPLY_CLICK_TYPE = "quick_reply_button"
_QUICK_REPLY_TYPES = {
    "quick_reply",
    "quick_reply_button",
    "text_back",
}
_URL_TYPES = {
    "url",
    "url_button",
    "visit_website",
}


def _aware(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return value.replace(tzinfo=dt_timezone.utc)
    return value


def _normalized_text(value):
    return " ".join(str(value or "").strip().split())


def _button_kind(value):
    return str(value or "").strip().lower()


def _accumulate_button_capability(capabilities, button):
    if not isinstance(button, dict):
        return
    kind = _button_kind(button.get("type"))
    if kind in _QUICK_REPLY_TYPES:
        capabilities["quick_reply"] = True
    elif kind in _URL_TYPES:
        capabilities["url"] = True


def _template_capabilities(template):
    capabilities = {"quick_reply": False, "url": False}

    for button in template.buttons or []:
        _accumulate_button_capability(capabilities, button)

    try:
        state = template.meta_state
    except (AttributeError, WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist):
        state = None

    components = (
        state.components
        if state is not None and isinstance(state.components, list)
        else []
    )
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").strip().upper() != "BUTTONS":
            continue
        for button in component.get("buttons") or []:
            _accumulate_button_capability(capabilities, button)

    carousel = (
        state.carousel_config
        if state is not None and isinstance(state.carousel_config, dict)
        else {}
    )
    for kind in carousel.get("button_types") or []:
        _accumulate_button_capability(capabilities, {"type": kind})
    for card in carousel.get("cards") or []:
        if not isinstance(card, dict):
            continue
        for button in card.get("buttons") or []:
            _accumulate_button_capability(capabilities, button)

    return capabilities


def _snapshot_capabilities(media_payload):
    capabilities = {"quick_reply": False, "url": False}
    if not isinstance(media_payload, dict):
        return capabilities
    display = media_payload.get("template_display")
    if not isinstance(display, dict):
        return capabilities
    for button in display.get("buttons") or []:
        _accumulate_button_capability(capabilities, button)
    for card in display.get("cards") or []:
        if not isinstance(card, dict):
            continue
        for button in card.get("buttons") or []:
            _accumulate_button_capability(capabilities, button)
    return capabilities


def extract_quick_reply_receipt(raw_payload):
    """Return a contextual quick-reply receipt or ``None``.

    A template quick reply arrives as ``type=button``. Some interactive reply
    payloads use ``type=interactive`` with ``button_reply``. In both cases the
    top-level ``context.id`` identifies the outbound message containing the
    tapped button; without that identifier the event cannot be attributed to a
    template safely and is ignored.
    """

    if not isinstance(raw_payload, dict):
        return None
    context = raw_payload.get("context")
    if not isinstance(context, dict):
        return None
    source_message_id = _normalized_text(context.get("id"))
    if not source_message_id:
        return None

    message_type = _button_kind(raw_payload.get("type"))
    content = ""
    if message_type == "button":
        button = raw_payload.get("button")
        if isinstance(button, dict):
            content = _normalized_text(button.get("text") or button.get("payload"))
    elif message_type == "interactive":
        interactive = raw_payload.get("interactive")
        if not isinstance(interactive, dict):
            return None
        if _button_kind(interactive.get("type")) != "button_reply":
            return None
        reply = interactive.get("button_reply")
        if isinstance(reply, dict):
            content = _normalized_text(reply.get("title") or reply.get("id"))
    else:
        return None

    return {
        "source_message_id": source_message_id,
        "button_type": QUICK_REPLY_CLICK_TYPE,
        "button_content": content or "Quick reply",
    }


def _template_identity(payload, *, local_id_to_meta, name_to_meta, allowed_ids):
    if not isinstance(payload, dict) or payload.get("transport") != "template":
        return ""

    direct = _normalized_text(payload.get("meta_template_id"))
    if direct in allowed_ids:
        return direct

    mapped = local_id_to_meta.get(_normalized_text(payload.get("template_id")))
    if mapped in allowed_ids:
        return mapped

    candidates = name_to_meta.get(_normalized_text(payload.get("template_name")), ())
    if len(candidates) == 1 and candidates[0] in allowed_ids:
        return candidates[0]
    return ""


def _click_breakdown(mapping):
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
        key=lambda item: (
            -item["count"],
            item["button_content"],
            item["type"],
        )
    )
    return rows


def _unique_breakdown(mapping):
    rows = [
        {
            "type": key[0],
            "button_content": key[1],
            "count": len(values),
        }
        for key, values in mapping.items()
        if values
    ]
    rows.sort(
        key=lambda item: (
            -item["count"],
            item["button_content"],
            item["type"],
        )
    )
    return rows


def augment_local_click_receipts(
    *,
    account,
    template_ids,
    start_date,
    end_date,
    local_results,
):
    """Add locally observed quick-reply clicks to normalized analytics.

    Clicks are attributed to the outbound template's send day, matching the
    template analytics cohort. Only replies received within Meta's seven-day
    read/click attribution window are included.
    """

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
    local_id_to_meta = {
        str(template.pk): str(template.meta_template_id)
        for template in templates
        if template.meta_template_id
    }
    name_to_meta = defaultdict(list)
    capabilities = {}
    for template in templates:
        meta_id = str(template.meta_template_id or "")
        if not meta_id:
            continue
        name_to_meta[_normalized_text(template.name)].append(meta_id)
        capabilities[meta_id] = _template_capabilities(template)

    start_at = datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc)
    end_at = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=dt_timezone.utc,
    )
    successful_statuses = (
        WhatsAppMessage.Status.SENT,
        WhatsAppMessage.Status.DELIVERED,
        WhatsAppMessage.Status.READ,
    )
    outbound_rows = (
        WhatsAppMessage.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            external_id__isnull=False,
        )
        .filter(
            Q(sent_at__gte=start_at, sent_at__lt=end_at)
            | Q(
                sent_at__isnull=True,
                created_at__gte=start_at,
                created_at__lt=end_at,
                status__in=successful_statuses,
            )
        )
        .values(
            "external_id",
            "to_number",
            "sent_at",
            "created_at",
            "media_payload",
        )
    )

    outbound_by_context_id = {}
    for row in outbound_rows.iterator(chunk_size=1000):
        payload = row.get("media_payload")
        meta_id = _template_identity(
            payload,
            local_id_to_meta=local_id_to_meta,
            name_to_meta=name_to_meta,
            allowed_ids=set(ids),
        )
        if not meta_id or meta_id not in local_results:
            continue

        activity_at = _aware(row.get("sent_at") or row.get("created_at"))
        if activity_at is None:
            continue
        day_key = activity_at.astimezone(dt_timezone.utc).date().isoformat()
        external_id = _normalized_text(row.get("external_id"))
        if not external_id:
            continue

        snapshot = _snapshot_capabilities(payload)
        current = capabilities.setdefault(meta_id, {"quick_reply": False, "url": False})
        current["quick_reply"] = current["quick_reply"] or snapshot["quick_reply"]
        current["url"] = current["url"] or snapshot["url"]
        outbound_by_context_id[external_id] = {
            "meta_template_id": meta_id,
            "day": day_key,
            "sent_at": activity_at,
            "recipient": _normalized_text(row.get("to_number")),
        }

    day_maps = {
        meta_id: {
            str(row.get("date")): row
            for row in (result.get("days") or [])
            if row.get("date")
        }
        for meta_id, result in local_results.items()
    }
    click_counts = {meta_id: defaultdict(int) for meta_id in ids}
    unique_clicks = {
        meta_id: defaultdict(set)
        for meta_id in ids
    }

    if outbound_by_context_id:
        scan_end = min(
            timezone.now() + timedelta(seconds=1),
            end_at + timedelta(days=CLICK_ATTRIBUTION_DAYS),
        )
        inbound_rows = WhatsAppMessage.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            direction=WhatsAppMessage.Direction.INBOUND,
            created_at__gte=start_at,
            created_at__lt=scan_end,
        ).values(
            "external_id",
            "from_number",
            "created_at",
            "raw_payload",
        )

        for row in inbound_rows.iterator(chunk_size=1000):
            receipt = extract_quick_reply_receipt(row.get("raw_payload"))
            if not receipt:
                continue
            outbound = outbound_by_context_id.get(receipt["source_message_id"])
            if not outbound:
                continue

            clicked_at = _aware(row.get("created_at"))
            sent_at = outbound["sent_at"]
            if clicked_at is None or clicked_at < sent_at:
                continue
            if clicked_at > sent_at + timedelta(days=CLICK_ATTRIBUTION_DAYS):
                continue

            meta_id = outbound["meta_template_id"]
            result = local_results.get(meta_id)
            if not result:
                continue
            day_row = day_maps.get(meta_id, {}).get(outbound["day"])
            if day_row is None:
                continue

            day_row["clicked"] = _base._as_int(day_row.get("clicked")) + 1
            key = (receipt["button_type"], receipt["button_content"])
            click_counts[meta_id][key] += 1
            identity = (
                _normalized_text(row.get("from_number"))
                or outbound["recipient"]
                or _normalized_text(row.get("external_id"))
            )
            if identity:
                unique_clicks[meta_id][key].add(identity)

    for meta_id, result in local_results.items():
        capability = capabilities.get(meta_id) or {"quick_reply": False, "url": False}
        if not capability["quick_reply"]:
            continue

        availability = result.setdefault("availability", {})
        availability["clicked"] = True
        availability["unique_clicked"] = True
        result["clicks"] = _click_breakdown(click_counts.get(meta_id, {}))
        result["unique_clicks"] = _unique_breakdown(unique_clicks.get(meta_id, {}))
        clicked_total = sum(
            _base._as_int(row.get("clicked"))
            for row in result.get("days") or []
        )
        unique_total = sum(
            _base._as_int(row.get("count"))
            for row in result["unique_clicks"]
        )
        result.setdefault("totals", {})["clicked"] = clicked_total
        result.setdefault("rates", {})["clicked"] = _base._rate(
            clicked_total,
            (result.get("totals") or {}).get("delivered", 0),
        )
        result["unique_click_total"] = unique_total
        result["unique_click_rate"] = _base._rate(
            unique_total,
            (result.get("totals") or {}).get("delivered", 0),
        )
        result["click_count_basis"] = "local_quick_reply"
        result["click_source"] = "shvya_quick_reply_receipts"
        result["click_source_label"] = "SHVYA quick-reply receipts"
        result["click_scope"] = (
            "quick_reply_only"
            if capability["url"]
            else "quick_reply"
        )
        result["click_data_partial"] = bool(capability["url"])
        result["local_click_receipts_available"] = True
        result["local_click_receipts_applied"] = clicked_total > 0
        if result.get("source") == "shvya":
            result["source_label"] = "SHVYA delivery and quick-reply receipts"

    return local_results


def _canonical_breakdown_key(row):
    kind = _button_kind((row or {}).get("type"))
    if kind.startswith("unique_"):
        kind = kind.removeprefix("unique_")
    if kind in {"quick_reply", "text_back"}:
        kind = QUICK_REPLY_CLICK_TYPE
    content = _normalized_text((row or {}).get("button_content")).casefold()
    return kind or "button", content


def _merge_breakdowns(provider_rows, local_rows):
    merged = {}
    for source, rows in (("provider", provider_rows), ("local", local_rows)):
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            count = _base._as_int(row.get("count"))
            if count <= 0:
                continue
            key = _canonical_breakdown_key(row)
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "type": key[0],
                    "button_content": _normalized_text(row.get("button_content")),
                    "count": count,
                    "source": source,
                }
                continue
            if count > existing["count"]:
                existing["count"] = count
            if source == "provider" and _normalized_text(row.get("button_content")):
                existing["button_content"] = _normalized_text(row.get("button_content"))
                existing["source"] = source

    rows = [
        {
            "type": item["type"],
            "button_content": item["button_content"],
            "count": item["count"],
        }
        for item in merged.values()
    ]
    rows.sort(
        key=lambda item: (
            -item["count"],
            item["button_content"],
            item["type"],
        )
    )
    return rows


def merge_local_click_receipts(*, meta_results, local_results):
    """Use local quick-reply receipts when Meta omits or delays click data."""

    for meta_id, meta_result in meta_results.items():
        local_result = local_results.get(meta_id)
        if not local_result:
            continue
        local_availability = local_result.get("availability") or {}
        if not local_availability.get("clicked"):
            continue

        meta_availability = meta_result.setdefault("availability", {})
        provider_had_clicks = bool(meta_availability.get("clicked"))
        local_days = {
            row.get("date"): row
            for row in local_result.get("days") or []
            if row.get("date")
        }
        augmented = False
        for meta_day in meta_result.get("days") or []:
            local_day = local_days.get(meta_day.get("date"))
            if not local_day:
                continue
            local_count = _base._as_int(local_day.get("clicked"))
            if local_count > _base._as_int(meta_day.get("clicked")):
                meta_day["clicked"] = local_count
                augmented = True

        if not provider_had_clicks:
            meta_availability["clicked"] = True
            augmented = True
        meta_result["clicks"] = _merge_breakdowns(
            meta_result.get("clicks") or [],
            local_result.get("clicks") or [],
        )
        if local_availability.get("unique_clicked"):
            meta_availability["unique_clicked"] = True
            meta_result["unique_clicks"] = _merge_breakdowns(
                meta_result.get("unique_clicks") or [],
                local_result.get("unique_clicks") or [],
            )
            meta_result["unique_click_total"] = sum(
                _base._as_int(row.get("count"))
                for row in meta_result["unique_clicks"]
            )

        _base._refresh_totals_and_rates(meta_result)
        breakdown_total = sum(
            _base._as_int(row.get("count"))
            for row in meta_result.get("clicks") or []
        )
        if breakdown_total > _base._as_int(meta_result["totals"].get("clicked")):
            meta_result["totals"]["clicked"] = breakdown_total
            meta_result["rates"]["clicked"] = _base._rate(
                breakdown_total,
                meta_result["totals"].get("delivered", 0),
            )
            augmented = True

        if meta_availability.get("unique_clicked"):
            meta_result["unique_click_rate"] = _base._rate(
                meta_result.get("unique_click_total", 0),
                meta_result["totals"].get("delivered", 0),
            )
        else:
            meta_result["unique_click_rate"] = None

        if augmented:
            meta_result["source"] = "meta+shvya"
            meta_result["source_label"] = (
                "Meta insights + SHVYA delivery and quick-reply receipts"
            )
            meta_result["click_source"] = (
                "meta+shvya"
                if provider_had_clicks
                else local_result.get("click_source", "shvya_quick_reply_receipts")
            )
            meta_result["click_source_label"] = (
                "Meta insights + SHVYA quick-reply receipts"
                if provider_had_clicks
                else local_result.get(
                    "click_source_label",
                    "SHVYA quick-reply receipts",
                )
            )
            meta_result["click_count_basis"] = (
                "meta_with_local_quick_reply_floor"
                if provider_had_clicks
                else local_result.get("click_count_basis", "local_quick_reply")
            )
            meta_result["click_scope"] = (
                meta_result.get("click_scope")
                or local_result.get("click_scope", "quick_reply")
            )
            meta_result["click_data_partial"] = bool(
                not provider_had_clicks
                and local_result.get("click_data_partial")
            )
            meta_result["local_click_receipts_available"] = True
            meta_result["local_click_receipts_applied"] = True

    return meta_results
