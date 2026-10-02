"""Hardening for tracked WhatsApp template CTA actions.

Carousel templates are always Marketing templates. Their local card actions must
remain available after a Meta sync because Meta only returns the SHVYA tracking
URL, not the server-held final website or phone action. Meta click tracking is
also enabled before the first analytics read.
"""

import copy

from apps.channels.models import WhatsAppTemplate


_INSTALLED = False


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

    current_carousel_button = template_service._carousel_button_payload
    current_fetch_analytics = template_analytics.fetch_template_analytics
    current_sync_templates = template_meta_fix.sync_templates

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

    def fetch_analytics(*, account, template_ids, start_date, end_date):
        # Confirm Meta's own URL tracking before the read so the first insights
        # request does not unnecessarily return an unavailable click field.
        templates = WhatsAppTemplate.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=template_ids,
            status=WhatsAppTemplate.Status.APPROVED,
        ).select_related("account")
        for template in templates:
            tracking._enable_meta_click_tracking(template)
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

    template_service._carousel_button_payload = carousel_button
    template_analytics.fetch_template_analytics = fetch_analytics
    template_meta_fix.sync_templates = sync_templates
    _INSTALLED = True
