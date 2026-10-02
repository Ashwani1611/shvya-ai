"""Hardening for tracked WhatsApp template CTA actions.

Carousel templates are always Marketing templates. Their local card actions must
remain available after a Meta sync because Meta only returns the SHVYA tracking
URL, not the server-held final website or phone action. Meta click tracking is
also enabled before the first analytics read, but only for templates that
actually contain URL actions. Total and unique tracked clicks are merged per
button label so Meta and SHVYA never count the same action twice.
"""

import copy
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.db.models import Count, Q

from apps.channels.models import WhatsAppTemplate
from apps.channels.tracking_models import (
    WhatsAppTemplateTrackedClick,
    WhatsAppTemplateTrackedLink,
)


_INSTALLED = False
_META_DYNAMIC_URL_EXAMPLE_SUFFIX = "00000000-0000-4000-8000-000000000001"


def _components_have_tracking(components, *, checker):
    for component in components or []:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").upper()
        if kind == "BUTTONS" and any(
            isinstance(button, dict) and checker(button.get("url"))
            for button in component.get("buttons") or []
        ):
            return True
        if kind == "CAROUSEL":
            for card in component.get("cards") or []:
                if isinstance(card, dict) and _components_have_tracking(
                    card.get("components") or [],
                    checker=checker,
                ):
                    return True
    return False


def install_template_cta_tracking_hardening():
    global _INSTALLED
    if _INSTALLED:
        return

    from . import template_analytics
    from . import template_cta_tracking as tracking
    from . import template_meta_fix
    from . import template_service

    current_augment_analytics = tracking.augment_tracked_cta_analytics
    current_carousel_button = template_service._carousel_button_payload
    current_enable_meta_click_tracking = tracking._enable_meta_click_tracking
    current_fetch_analytics = template_analytics.fetch_template_analytics
    current_sync_templates = template_meta_fix.sync_templates

    def tracking_example_suffix():
        # Meta's dynamic URL template example is the variable suffix only, not
        # the fully expanded URL. The send-time parameter uses the same shape.
        return _META_DYNAMIC_URL_EXAMPLE_SUFFIX

    def enable_meta_click_tracking(template):
        try:
            state = template.meta_state
        except (
            AttributeError,
            WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist,
        ):
            return False
        components = state.components if isinstance(state.components, list) else []
        if not _components_have_tracking(
            components,
            checker=lambda value: bool(str(value or "").strip()),
        ):
            return False
        return current_enable_meta_click_tracking(template)

    def carousel_button(button):
        # Carousel templates are Marketing-only. Website and Call actions can
        # therefore be converted directly to the same per-send tracked URL
        # used by standard Marketing/Utility templates.
        if (
            isinstance(button, dict)
            and str(button.get("type") or "") in {"visit_website", "call_phone"}
        ):
            return tracking._tracked_meta_button(button)
        return current_carousel_button(button)

    def augment_analytics(
        *,
        account,
        template_ids,
        start_date,
        end_date,
        results,
    ):
        # Preserve provider and quick-reply metrics before the core tracked
        # augmentation. The core layer establishes availability/source metadata;
        # this layer recomputes tracked URL totals from the original rows plus
        # recipient-linked SHVYA events, using a per-label maximum. Provider
        # aggregate totals remain a floor when no per-button breakdown exists.
        existing_clicks = {
            str(meta_id): copy.deepcopy(result.get("clicks") or [])
            for meta_id, result in (results or {}).items()
        }
        existing_unique = {
            str(meta_id): copy.deepcopy(result.get("unique_clicks") or [])
            for meta_id, result in (results or {}).items()
        }
        existing_clicked_totals = {
            str(meta_id): int((result.get("totals") or {}).get("clicked") or 0)
            for meta_id, result in (results or {}).items()
        }
        existing_unique_totals = {
            str(meta_id): int(result.get("unique_click_total") or 0)
            for meta_id, result in (results or {}).items()
        }
        augmented = current_augment_analytics(
            account=account,
            template_ids=template_ids,
            start_date=start_date,
            end_date=end_date,
            results=results,
        )

        ids = list(dict.fromkeys(str(value) for value in template_ids if value))
        if not ids or not augmented:
            return augmented
        start_at = datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc)
        end_at = datetime.combine(
            end_date + timedelta(days=1),
            time.min,
            tzinfo=dt_timezone.utc,
        )
        totals = {
            meta_id: defaultdict(int)
            for meta_id in ids
        }
        recipients = {
            meta_id: defaultdict(set)
            for meta_id in ids
        }
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
                tracked_click_total=Count(
                    "events",
                    filter=Q(
                        events__event_type=(
                            WhatsAppTemplateTrackedClick.EventType.CLICK
                        )
                    ),
                )
            )
            .select_related("message")
        )
        for link in links:
            meta_id = str(link.meta_template_id or "")
            if meta_id not in augmented:
                continue
            # All independently tracked actions are URL buttons in the Meta
            # template. Keeping that canonical type makes Meta and SHVYA rows
            # for Call/Copy landing actions merge instead of double count.
            key = (
                "url_button",
                link.button_text or "CTA button",
            )
            click_total = int(link.tracked_click_total or 0)
            totals[meta_id][key] += click_total
            if click_total <= 0:
                continue
            identity = (
                str(link.lead_id or "")
                or str(link.message.to_number or "").strip()
                or str(link.message_id)
            )
            recipients[meta_id][key].add(identity)

        for meta_id, result in augmented.items():
            local_rows = [
                {
                    "type": key[0],
                    "button_content": key[1],
                    "count": count,
                }
                for key, count in totals.get(meta_id, {}).items()
                if count > 0
            ]
            local_unique_rows = [
                {
                    "type": key[0],
                    "button_content": key[1],
                    "count": len(values),
                }
                for key, values in recipients.get(meta_id, {}).items()
                if values
            ]
            result["clicks"] = tracking._merge_breakdowns(
                existing_clicks.get(meta_id, []),
                local_rows,
            )
            result["unique_clicks"] = tracking._merge_breakdowns(
                existing_unique.get(meta_id, []),
                local_unique_rows,
            )
            breakdown_total = sum(
                int(row.get("count") or 0)
                for row in result["clicks"]
            )
            unique_breakdown_total = sum(
                int(row.get("count") or 0)
                for row in result["unique_clicks"]
            )
            clicked_total = max(
                existing_clicked_totals.get(meta_id, 0),
                breakdown_total,
            )
            unique_total = max(
                existing_unique_totals.get(meta_id, 0),
                unique_breakdown_total,
            )
            delivered = int((result.get("totals") or {}).get("delivered") or 0)
            result.setdefault("totals", {})["clicked"] = clicked_total
            result.setdefault("rates", {})["clicked"] = (
                round((clicked_total / delivered) * 100, 1)
                if delivered
                else None
            )
            result["unique_click_total"] = unique_total
            result["unique_click_rate"] = (
                round((unique_total / delivered) * 100, 1)
                if delivered
                else None
            )
        return augmented

    def fetch_analytics(*, account, template_ids, start_date, end_date):
        # Confirm Meta's own URL tracking before the read so the first insights
        # request does not unnecessarily return an unavailable click field.
        templates = WhatsAppTemplate.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=template_ids,
            status=WhatsAppTemplate.Status.APPROVED,
        ).select_related("account", "meta_state")
        for template in templates:
            enable_meta_click_tracking(template)
        return current_fetch_analytics(
            account=account,
            template_ids=template_ids,
            start_date=start_date,
            end_date=end_date,
        )

    def sync_templates(*, organization, account):
        preserved = {}
        queryset = WhatsAppTemplate.objects.filter(
            organization=organization,
            account=account,
            template_format=WhatsAppTemplate.Format.CAROUSEL,
        ).select_related("meta_state")
        for template in queryset:
            try:
                config = template.meta_state.carousel_config
            except (
                AttributeError,
                WhatsAppTemplate.meta_state.RelatedObjectDoesNotExist,
            ):
                continue
            if isinstance(config, dict) and config:
                preserved[template.pk] = copy.deepcopy(config)

        summary = current_sync_templates(
            organization=organization,
            account=account,
        )

        if not preserved:
            return summary
        refreshed = WhatsAppTemplate.objects.filter(
            pk__in=preserved,
            organization=organization,
            account=account,
        ).select_related("meta_state")
        for template in refreshed:
            state = template.meta_state
            if not _components_have_tracking(
                state.components,
                checker=tracking.is_tracking_url,
            ):
                continue
            state.carousel_config = preserved[template.pk]
            state.save(update_fields=["carousel_config", "updated_at"])
        return summary

    tracking.tracking_example_url = tracking_example_suffix
    tracking._enable_meta_click_tracking = enable_meta_click_tracking
    template_service._carousel_button_payload = carousel_button
    template_analytics.fetch_template_analytics = fetch_analytics
    tracking.augment_tracked_cta_analytics = augment_analytics
    template_meta_fix.sync_templates = sync_templates
    _INSTALLED = True
