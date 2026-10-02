"""Resilient analytics for WhatsApp message templates.

The provider/base implementation remains in ``template_analytics_base`` so this
module can keep the stable import path while applying SHVYA's canonical click
semantics. Meta can return total and unique rows for the same button in one
``clicked`` array. Total clicks drive the existing Button clicks KPI; unique
rows are exposed separately and are never added to the total.
"""

from collections import defaultdict

from . import template_analytics_base as _base


# Preserve the existing module API for callers and tests while overriding the
# click-specific normalization below.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)
del _name


def _canonical_click_type(value):
    """Return ``(canonical_type, is_unique)`` for a Meta click metric row."""

    metric_type = str(value or "button").strip().lower() or "button"
    is_unique = metric_type.startswith("unique_")
    if is_unique:
        metric_type = metric_type.removeprefix("unique_") or "button"
    return metric_type, is_unique


def _clicked_rows(value):
    """Normalize Meta click rows without mixing total and unique semantics."""

    if isinstance(value, (int, float, str)):
        count = _base._as_int(value)
        return (
            [
                {
                    "type": "button",
                    "button_content": "",
                    "count": count,
                    "is_unique": False,
                }
            ]
            if count
            else []
        )
    if not isinstance(value, list):
        return []

    rows = []
    for item in value:
        if not isinstance(item, dict):
            continue
        count = _base._as_int(item.get("count"))
        if count <= 0:
            continue
        metric_type, is_unique = _canonical_click_type(item.get("type"))
        rows.append(
            {
                "type": metric_type,
                "button_content": str(item.get("button_content") or "").strip(),
                "count": count,
                "is_unique": is_unique,
            }
        )
    return rows


def _select_counted_click_rows(rows):
    """Choose the rows that represent the Button clicks KPI.

    Meta may return ``url_button`` and ``unique_url_button`` together. Adding
    both doubles the displayed count. Prefer total rows. If a provider payload
    contains only unique rows, use those as an explicit fallback rather than
    showing an incorrect zero.
    """

    total_rows = [row for row in rows if not row["is_unique"]]
    unique_rows = [row for row in rows if row["is_unique"]]
    if total_rows:
        return total_rows, unique_rows, "total"
    if unique_rows:
        return unique_rows, unique_rows, "unique_fallback"
    return [], [], "total"


def _add_click_rows(target, rows):
    for row in rows:
        key = (row["type"], row["button_content"])
        target[key] += row["count"]


def _click_breakdown(mapping):
    rows = [
        {"type": key[0], "button_content": key[1], "count": count}
        for key, count in mapping.items()
    ]
    rows.sort(
        key=lambda item: (
            -item["count"],
            item["button_content"],
            item["type"],
        )
    )
    return rows


def _refresh_unique_click_rate(result):
    availability = result.setdefault("availability", {})
    if availability.get("unique_clicked"):
        result["unique_click_rate"] = _base._rate(
            result.get("unique_click_total", 0),
            (result.get("totals") or {}).get("delivered", 0),
        )
    else:
        result["unique_click_rate"] = None
    return result


def _fetch_meta_template_analytics(*, account, template_ids, start_date, end_date):
    results, daily_maps, click_maps = _base._initial_result_maps(
        template_ids,
        start_date,
        end_date,
    )
    unique_click_maps = {
        template_id: defaultdict(int)
        for template_id in template_ids
    }
    click_bases = {
        template_id: set()
        for template_id in template_ids
    }

    groups = _base._request_all_meta_groups(
        account=account,
        template_ids=template_ids,
        start_date=start_date,
        end_date=end_date,
    )
    for group in groups:
        if not isinstance(group, dict):
            continue
        for point in group.get("data_points") or []:
            if not isinstance(point, dict):
                continue
            template_id = str(
                point.get("template_id")
                or group.get("template_id")
                or ""
            )
            if template_id not in results:
                continue
            point_date = _base._date_from_point(point.get("start"))
            if (
                point_date is None
                or point_date < start_date
                or point_date > end_date
            ):
                continue

            click_total = 0
            if "clicked" in point:
                results[template_id]["availability"]["clicked"] = True
                normalized = _clicked_rows(point.get("clicked"))
                counted_rows, unique_rows, basis = _select_counted_click_rows(
                    normalized
                )
                click_bases[template_id].add(basis)
                click_total = sum(row["count"] for row in counted_rows)
                _add_click_rows(click_maps[template_id], counted_rows)
                if unique_rows:
                    results[template_id]["availability"][
                        "unique_clicked"
                    ] = True
                    _add_click_rows(
                        unique_click_maps[template_id],
                        unique_rows,
                    )

            row = {
                "date": point_date.isoformat(),
                "sent": _base._as_int(point.get("sent")),
                "delivered": _base._as_int(point.get("delivered")),
                "read": _base._as_int(point.get("read")),
                "clicked": click_total,
            }
            existing = daily_maps[template_id].get(row["date"])
            if existing:
                existing["sent"] += row["sent"]
                existing["delivered"] += row["delivered"]
                existing["read"] += row["read"]
                existing["clicked"] += row["clicked"]
            else:
                daily_maps[template_id][row["date"]] = row

    finalized = _base._finalize_results(
        results=results,
        daily_maps=daily_maps,
        click_maps=click_maps,
        source="meta",
        source_label="Meta template insights",
    )
    for template_id, result in finalized.items():
        unique_rows = _click_breakdown(unique_click_maps[template_id])
        result["unique_clicks"] = unique_rows
        result["unique_click_total"] = sum(
            row["count"] for row in unique_rows
        )
        bases = click_bases[template_id]
        if bases == {"total"} or not bases:
            result["click_count_basis"] = "total"
        elif bases == {"unique_fallback"}:
            result["click_count_basis"] = "unique_fallback"
        else:
            result["click_count_basis"] = "mixed"
        _refresh_unique_click_rate(result)
    return finalized


def fetch_template_analytics(*, account, template_ids, start_date, end_date):
    """Return Meta analytics with accurate total and unique click handling."""

    ids = _base._validate_request(
        template_ids=template_ids,
        start_date=start_date,
        end_date=end_date,
    )
    if not ids:
        return {}

    local_results = _base.fetch_local_template_analytics(
        account=account,
        template_ids=ids,
        start_date=start_date,
        end_date=end_date,
    )
    try:
        if not account.waba_id or not account.access_token:
            raise _base.TemplateAnalyticsError(
                "This WhatsApp business is missing the Meta credentials required for analytics."
            )
        meta_results = _fetch_meta_template_analytics(
            account=account,
            template_ids=ids,
            start_date=start_date,
            end_date=end_date,
        )
        merged = _base._merge_local_receipt_floor(
            meta_results=meta_results,
            local_results=local_results,
        )
        for result in merged.values():
            _refresh_unique_click_rate(result)
        return merged
    except _base.TemplateAnalyticsError as exc:
        for result in local_results.values():
            result["provider_warning"] = str(exc)
            result["provider_error_code"] = exc.meta_error_code
        return local_results
