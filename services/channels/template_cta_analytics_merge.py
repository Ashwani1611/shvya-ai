"""Merge policy for SHVYA-tracked template CTA analytics."""

from __future__ import annotations

from . import template_analytics_base as base


def apply_confirmed_cta_authority(*, meta_results, local_results):
    """Use confirmed SHVYA actions instead of Meta URL-click duplicates.

    Website, Call and Copy Code buttons on a tracked template are represented to
    Meta as URL buttons that open SHVYA's confirmation page. Meta can therefore
    count the initial URL open while SHVYA records the confirmed destination
    action. Summing both would count one recipient action twice and could also
    include link previews. For tracked templates, local contextual quick-reply
    receipts plus confirmed CTA events are the authoritative click series;
    Meta remains authoritative for sent, delivered and read metrics.
    """

    for template_id, meta_result in meta_results.items():
        local_result = local_results.get(template_id)
        if not local_result:
            continue
        if local_result.get("click_scope") != "all_tracked_buttons":
            continue

        local_days = {
            row.get("date"): row
            for row in local_result.get("days") or []
            if row.get("date")
        }
        for meta_day in meta_result.get("days") or []:
            local_day = local_days.get(meta_day.get("date"))
            meta_day["clicked"] = base._as_int(
                (local_day or {}).get("clicked")
            )

        availability = meta_result.setdefault("availability", {})
        availability["clicked"] = True
        availability["unique_clicked"] = True
        meta_result["clicks"] = list(local_result.get("clicks") or [])
        meta_result["unique_clicks"] = list(
            local_result.get("unique_clicks") or []
        )
        meta_result["unique_click_total"] = base._as_int(
            local_result.get("unique_click_total")
        )
        base._refresh_totals_and_rates(meta_result)
        meta_result["unique_click_rate"] = base._rate(
            meta_result["unique_click_total"],
            meta_result.get("totals", {}).get("delivered", 0),
        )
        meta_result["click_count_basis"] = local_result.get(
            "click_count_basis",
            "shvya_tracked_cta",
        )
        meta_result["click_source"] = "shvya_cta_receipts"
        meta_result["click_source_label"] = "SHVYA confirmed CTA receipts"
        meta_result["click_scope"] = "all_tracked_buttons"
        meta_result["click_data_partial"] = False
        meta_result["local_click_receipts_available"] = True
        meta_result["local_click_receipts_applied"] = bool(
            local_result.get("local_click_receipts_applied")
        )
        if str(meta_result.get("source") or "").startswith("meta"):
            meta_result["source"] = "meta+shvya"
            meta_result["source_label"] = (
                "Meta delivery/read insights + SHVYA confirmed CTA receipts"
            )
        else:
            meta_result["source"] = "shvya"
            meta_result["source_label"] = "SHVYA delivery and CTA receipts"

    return meta_results
