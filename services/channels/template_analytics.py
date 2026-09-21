"""Meta-backed analytics for WhatsApp message templates.

All figures in this module come from Meta's template_analytics field. The
service never derives template performance from SHVYA webhook rows, so the
template screen cannot accidentally mix a partial local event history with
Meta's canonical aggregate.
"""

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.utils import timezone

from apps.channels.providers import whatsapp as meta


MAX_LOOKBACK_DAYS = 90
MAX_TEMPLATE_IDS_PER_META_REQUEST = 10
METRIC_TYPES = ("SENT", "DELIVERED", "READ", "CLICKED")


class TemplateAnalyticsError(Exception):
    """A safe, user-displayable Meta template analytics failure."""

    def __init__(self, message, *, status_code=None, meta_error_code=""):
        super().__init__(message)
        self.status_code = status_code
        self.meta_error_code = str(meta_error_code or "")


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _as_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _rate(numerator, denominator):
    denominator = _as_int(denominator)
    if denominator <= 0:
        return None
    return round((_as_int(numerator) / denominator) * 100, 1)


def _date_from_point(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) >= 10 and stripped[4:5] == "-" and stripped[7:8] == "-":
            try:
                return date.fromisoformat(stripped[:10])
            except ValueError:
                return None
        try:
            value = int(float(stripped))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=dt_timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _meta_error(response):
    message = "Meta could not provide template analytics right now."
    code = ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = (
            error.get("error_user_msg")
            or error.get("message")
            or error.get("error_user_title")
            or message
        )
        code = error.get("code") or error.get("error_subcode") or ""
    elif getattr(response, "text", ""):
        message = "Meta rejected the template analytics request."
    return TemplateAnalyticsError(
        str(message),
        status_code=getattr(response, "status_code", None),
        meta_error_code=code,
    )


def _analytics_field(*, start_date, end_date, template_ids):
    start_ts = int(
        datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc).timestamp()
    )
    # The UI treats both selected dates as inclusive. Meta's daily analytics
    # end boundary is exclusive, so request midnight after the selected end.
    end_ts = int(
        datetime.combine(
            end_date + timedelta(days=1),
            time.min,
            tzinfo=dt_timezone.utc,
        ).timestamp()
    )
    ids = json.dumps([str(value) for value in template_ids], separators=(",", ":"))
    metrics = json.dumps(list(METRIC_TYPES), separators=(",", ":"))
    return (
        "template_analytics"
        f".start({start_ts})"
        f".end({end_ts})"
        ".granularity(DAILY)"
        f".template_ids({ids})"
        f".metric_types({metrics})"
    )


def _request_batch(*, account, template_ids, start_date, end_date):
    field = _analytics_field(
        start_date=start_date,
        end_date=end_date,
        template_ids=template_ids,
    )
    try:
        response = meta.requests.get(
            f"{meta.GRAPH_API_BASE}/{account.waba_id}",
            headers={"Authorization": f"Bearer {account.access_token}"},
            params={"fields": field},
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        raise TemplateAnalyticsError(
            f"Network error loading Meta template analytics: {exc}"
        ) from exc

    if not response.ok:
        raise _meta_error(response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise TemplateAnalyticsError(
            "Meta template analytics returned invalid JSON.",
            status_code=response.status_code,
        ) from exc

    analytics = payload.get("template_analytics") if isinstance(payload, dict) else None
    if analytics is None and isinstance(payload, dict):
        analytics = payload
    if not isinstance(analytics, dict):
        return []

    groups = analytics.get("data")
    if isinstance(groups, list):
        return groups
    if isinstance(analytics.get("data_points"), list):
        return [analytics]
    return []


def _clicked_rows(value):
    if isinstance(value, (int, float, str)):
        count = _as_int(value)
        return ([{"type": "button", "button_content": "", "count": count}] if count else [])
    if not isinstance(value, list):
        return []
    rows = []
    for item in value:
        if not isinstance(item, dict):
            continue
        count = _as_int(item.get("count"))
        if count <= 0:
            continue
        rows.append(
            {
                "type": str(item.get("type") or "button"),
                "button_content": str(item.get("button_content") or ""),
                "count": count,
            }
        )
    return rows


def _empty_template_result(template_id, start_date, end_date):
    days = []
    current = start_date
    while current <= end_date:
        days.append(
            {
                "date": current.isoformat(),
                "sent": 0,
                "delivered": 0,
                "read": 0,
                "clicked": 0,
            }
        )
        current += timedelta(days=1)
    return {
        "template_id": str(template_id),
        "days": days,
        "totals": {"sent": 0, "delivered": 0, "read": 0, "clicked": 0},
        "rates": {"delivered": None, "read": None, "clicked": None},
        "clicks": [],
    }


def fetch_template_analytics(*, account, template_ids, start_date, end_date):
    """Return normalized daily Meta analytics keyed by Meta template ID.

    The caller must already have tenant-scoped the account/template selection.
    This service verifies that the connected account has the provider
    credentials required for the read and never performs cross-account lookup.
    """

    ids = [str(value) for value in template_ids if value]
    if not ids:
        return {}

    if not account.waba_id or not account.access_token:
        raise TemplateAnalyticsError(
            "This WhatsApp business is missing the Meta credentials required for analytics."
        )
    if end_date < start_date:
        raise TemplateAnalyticsError("The analytics end date must be on or after the start date.")

    inclusive_days = (end_date - start_date).days + 1
    if inclusive_days < 1 or inclusive_days > MAX_LOOKBACK_DAYS:
        raise TemplateAnalyticsError(
            f"Template analytics supports a maximum {MAX_LOOKBACK_DAYS}-day range."
        )

    results = {
        template_id: _empty_template_result(template_id, start_date, end_date)
        for template_id in ids
    }
    daily_maps = {template_id: {} for template_id in ids}
    click_maps = {
        template_id: defaultdict(int)
        for template_id in ids
    }

    for batch in _chunks(ids, MAX_TEMPLATE_IDS_PER_META_REQUEST):
        groups = _request_batch(
            account=account,
            template_ids=batch,
            start_date=start_date,
            end_date=end_date,
        )
        for group in groups:
            if not isinstance(group, dict):
                continue
            for point in group.get("data_points") or []:
                if not isinstance(point, dict):
                    continue
                template_id = str(point.get("template_id") or "")
                if template_id not in results:
                    continue
                point_date = _date_from_point(point.get("start"))
                if point_date is None or point_date < start_date or point_date > end_date:
                    continue

                clicks = _clicked_rows(point.get("clicked"))
                click_total = sum(item["count"] for item in clicks)
                row = {
                    "date": point_date.isoformat(),
                    "sent": _as_int(point.get("sent")),
                    "delivered": _as_int(point.get("delivered")),
                    "read": _as_int(point.get("read")),
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

                for click in clicks:
                    key = (click["type"], click["button_content"])
                    click_maps[template_id][key] += click["count"]

    for template_id, result in results.items():
        normalized_days = []
        totals = {"sent": 0, "delivered": 0, "read": 0, "clicked": 0}
        for fallback in result["days"]:
            row = daily_maps[template_id].get(fallback["date"], fallback)
            normalized_days.append(row)
            for metric in totals:
                totals[metric] += _as_int(row.get(metric))

        clicks = [
            {"type": key[0], "button_content": key[1], "count": count}
            for key, count in click_maps[template_id].items()
        ]
        clicks.sort(key=lambda item: (-item["count"], item["button_content"], item["type"]))
        result["days"] = normalized_days
        result["totals"] = totals
        result["rates"] = {
            "delivered": _rate(totals["delivered"], totals["sent"]),
            "read": _rate(totals["read"], totals["delivered"]),
            "clicked": _rate(totals["clicked"], totals["delivered"]),
        }
        result["clicks"] = clicks

    fetched_at = timezone.now().isoformat()
    for result in results.values():
        result["fetched_at"] = fetched_at
    return results
