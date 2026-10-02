"""Resilient analytics for WhatsApp message templates.

Meta's ``template_analytics`` field is the preferred source because it can
provide canonical aggregate delivery, read, and click data. Some WhatsApp
Business Accounts, including coexistence accounts, return error 200007 until
Template Insights is enabled. SHVYA enables that field once and retries.

Meta analytics must not be a single point of failure for the template screen.
SHVYA's outbound-message rows and status webhooks provide an immediate,
tenant-scoped receipt floor for sent, delivered, and read figures. They fill
Meta reporting delay or provider failure without fabricating click metrics.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.db.models import Q
from django.utils import timezone

from apps.channels.models import WhatsAppMessage, WhatsAppTemplate
from apps.channels.providers import whatsapp as meta


MAX_LOOKBACK_DAYS = 90
MAX_TEMPLATE_IDS_PER_META_REQUEST = 10
METRIC_TYPES = ("SENT", "DELIVERED", "READ", "CLICKED")
TEMPLATE_INSIGHTS_NOT_ENABLED = "200007"


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
        # Meta commonly returns the generic Graph code in ``code`` and the
        # actionable WhatsApp analytics code (for example 200007) in
        # ``error_subcode``. Prefer the subcode so recovery is deterministic.
        code = error.get("error_subcode") or error.get("code") or ""
    elif getattr(response, "text", ""):
        message = "Meta rejected the template analytics request."
    return TemplateAnalyticsError(
        str(message),
        status_code=getattr(response, "status_code", None),
        meta_error_code=code,
    )


def _analytics_timestamps(start_date, end_date):
    start_ts = int(
        datetime.combine(start_date, time.min, tzinfo=dt_timezone.utc).timestamp()
    )
    # Use the final second of the selected end day so the UI's displayed range
    # is inclusive without asking Meta for the next calendar day's bucket.
    end_ts = int(
        datetime.combine(
            end_date + timedelta(days=1),
            time.min,
            tzinfo=dt_timezone.utc,
        ).timestamp()
    ) - 1
    return start_ts, end_ts


def _request_batch(*, account, template_ids, start_date, end_date):
    start_ts, end_ts = _analytics_timestamps(start_date, end_date)
    try:
        response = meta.requests.get(
            f"{meta.GRAPH_API_BASE}/{account.waba_id}/template_analytics",
            headers={"Authorization": f"Bearer {account.access_token}"},
            params={
                "start": start_ts,
                "end": end_ts,
                "granularity": "DAILY",
                "template_ids": [str(value) for value in template_ids],
                "metric_types": list(METRIC_TYPES),
            },
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


def _enable_template_insights(*, account):
    """Enable Meta Template Insights for a WABA after error 200007.

    This is attempted only after Meta explicitly says the feature is disabled;
    ordinary requests never mutate WABA settings.
    """

    try:
        response = meta.requests.post(
            f"{meta.GRAPH_API_BASE}/{account.waba_id}",
            headers={"Authorization": f"Bearer {account.access_token}"},
            params={"is_enabled_for_insights": "true"},
            timeout=meta.REQUEST_TIMEOUT_SECONDS,
        )
    except meta.requests.RequestException as exc:
        raise TemplateAnalyticsError(
            f"Network error enabling Meta template analytics: {exc}"
        ) from exc

    if not response.ok:
        raise _meta_error(response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise TemplateAnalyticsError(
            "Meta returned invalid JSON while enabling template analytics.",
            status_code=response.status_code,
        ) from exc

    if isinstance(payload, dict) and payload.get("success") is False:
        raise TemplateAnalyticsError(
            "Meta did not enable template analytics for this WhatsApp Business account.",
            status_code=response.status_code,
        )


def _request_all_meta_groups(*, account, template_ids, start_date, end_date):
    groups = []
    enable_attempted = False
    for batch in _chunks(template_ids, MAX_TEMPLATE_IDS_PER_META_REQUEST):
        try:
            batch_groups = _request_batch(
                account=account,
                template_ids=batch,
                start_date=start_date,
                end_date=end_date,
            )
        except TemplateAnalyticsError as exc:
            disabled_message = "not been enabled" in str(exc).casefold()
            if (
                (
                    exc.meta_error_code == TEMPLATE_INSIGHTS_NOT_ENABLED
                    or disabled_message
                )
                and not enable_attempted
            ):
                _enable_template_insights(account=account)
                enable_attempted = True
                batch_groups = _request_batch(
                    account=account,
                    template_ids=batch,
                    start_date=start_date,
                    end_date=end_date,
                )
            else:
                raise
        groups.extend(batch_groups)
    return groups


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
        "availability": {"clicked": False},
        "clicks": [],
    }


def _initial_result_maps(template_ids, start_date, end_date):
    results = {
        template_id: _empty_template_result(template_id, start_date, end_date)
        for template_id in template_ids
    }
    daily_maps = {template_id: {} for template_id in template_ids}
    click_maps = {template_id: defaultdict(int) for template_id in template_ids}
    return results, daily_maps, click_maps


def _refresh_totals_and_rates(result):
    totals = {"sent": 0, "delivered": 0, "read": 0, "clicked": 0}
    for row in result.get("days") or []:
        for metric in totals:
            totals[metric] += _as_int(row.get(metric))
    result["totals"] = totals
    result["rates"] = {
        "delivered": _rate(totals["delivered"], totals["sent"]),
        "read": _rate(totals["read"], totals["delivered"]),
        "clicked": _rate(totals["clicked"], totals["delivered"]),
    }
    return result


def _finalize_results(*, results, daily_maps, click_maps, source, source_label):
    for template_id, result in results.items():
        normalized_days = []
        for fallback in result["days"]:
            normalized_days.append(
                daily_maps[template_id].get(fallback["date"], fallback)
            )

        clicks = [
            {"type": key[0], "button_content": key[1], "count": count}
            for key, count in click_maps[template_id].items()
        ]
        clicks.sort(key=lambda item: (-item["count"], item["button_content"], item["type"]))
        result["days"] = normalized_days
        _refresh_totals_and_rates(result)
        result["clicks"] = clicks
        result["source"] = source
        result["source_label"] = source_label

    fetched_at = timezone.now().isoformat()
    for result in results.values():
        result["fetched_at"] = fetched_at
    return results


def _validate_request(*, template_ids, start_date, end_date):
    ids = list(dict.fromkeys(str(value) for value in template_ids if value))
    if not ids:
        return []
    if end_date < start_date:
        raise TemplateAnalyticsError("The analytics end date must be on or after the start date.")

    inclusive_days = (end_date - start_date).days + 1
    if inclusive_days < 1 or inclusive_days > MAX_LOOKBACK_DAYS:
        raise TemplateAnalyticsError(
            f"Template analytics supports a maximum {MAX_LOOKBACK_DAYS}-day range."
        )
    return ids


def fetch_local_template_analytics(*, account, template_ids, start_date, end_date):
    """Derive template delivery/read metrics from SHVYA's durable receipts."""

    ids = _validate_request(
        template_ids=template_ids,
        start_date=start_date,
        end_date=end_date,
    )
    if not ids:
        return {}

    results, daily_maps, click_maps = _initial_result_maps(
        ids,
        start_date,
        end_date,
    )
    templates = list(
        WhatsAppTemplate.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            meta_template_id__in=ids,
        ).only("id", "meta_template_id", "name")
    )
    local_id_to_meta = {
        str(template.pk): str(template.meta_template_id)
        for template in templates
        if template.meta_template_id
    }
    name_to_meta = defaultdict(list)
    for template in templates:
        if template.meta_template_id:
            name_to_meta[str(template.name or "")].append(str(template.meta_template_id))

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
    rows = (
        WhatsAppMessage.objects.filter(
            organization_id=account.organization_id,
            account_id=account.pk,
            direction=WhatsAppMessage.Direction.OUTBOUND,
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
        .values("status", "sent_at", "created_at", "media_payload")
    )

    for row in rows.iterator(chunk_size=1000):
        payload = row.get("media_payload")
        if not isinstance(payload, dict) or payload.get("transport") != "template":
            continue

        meta_template_id = local_id_to_meta.get(str(payload.get("template_id") or ""))
        if not meta_template_id:
            candidates = name_to_meta.get(str(payload.get("template_name") or ""), [])
            if len(candidates) == 1:
                meta_template_id = candidates[0]
        if meta_template_id not in results:
            continue

        activity_at = row.get("sent_at") or row.get("created_at")
        if activity_at is None:
            continue
        if timezone.is_naive(activity_at):
            activity_at = activity_at.replace(tzinfo=dt_timezone.utc)
        day_key = activity_at.astimezone(dt_timezone.utc).date().isoformat()
        bucket = daily_maps[meta_template_id].setdefault(
            day_key,
            {
                "date": day_key,
                "sent": 0,
                "delivered": 0,
                "read": 0,
                "clicked": 0,
            },
        )
        bucket["sent"] += 1
        status = row.get("status")
        if status in {
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
        }:
            bucket["delivered"] += 1
        if status == WhatsAppMessage.Status.READ:
            bucket["read"] += 1

    return _finalize_results(
        results=results,
        daily_maps=daily_maps,
        click_maps=click_maps,
        source="shvya",
        source_label="SHVYA delivery receipts",
    )


def _fetch_meta_template_analytics(*, account, template_ids, start_date, end_date):
    results, daily_maps, click_maps = _initial_result_maps(
        template_ids,
        start_date,
        end_date,
    )
    groups = _request_all_meta_groups(
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
            template_id = str(point.get("template_id") or group.get("template_id") or "")
            if template_id not in results:
                continue
            point_date = _date_from_point(point.get("start"))
            if point_date is None or point_date < start_date or point_date > end_date:
                continue

            if "clicked" in point:
                results[template_id]["availability"]["clicked"] = True
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

    return _finalize_results(
        results=results,
        daily_maps=daily_maps,
        click_maps=click_maps,
        source="meta",
        source_label="Meta template insights",
    )


def _merge_local_receipt_floor(*, meta_results, local_results):
    """Fill Meta reporting lag with known local receipts without summing twice."""

    for template_id, meta_result in meta_results.items():
        local_result = local_results.get(template_id)
        if not local_result:
            continue
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
            # Both sources represent the same sends. A per-day maximum is a
            # receipt floor, not a sum, so the same message is never counted
            # twice while delayed Meta aggregates are filled immediately.
            for metric in ("sent", "delivered", "read"):
                local_value = _as_int(local_day.get(metric))
                if local_value > _as_int(meta_day.get(metric)):
                    meta_day[metric] = local_value
                    augmented = True

        if augmented:
            _refresh_totals_and_rates(meta_result)
            meta_result["source"] = "meta+shvya"
            meta_result["source_label"] = "Meta insights + SHVYA delivery receipts"
            meta_result["local_receipts_applied"] = True
    return meta_results


def fetch_template_analytics(*, account, template_ids, start_date, end_date):
    """Return analytics keyed by Meta template ID with immediate local receipts.

    Meta is attempted first. Error 200007 triggers a one-time WABA Template
    Insights enablement and retry. SHVYA receipts then fill any reporting lag
    using a per-day maximum. Any remaining provider, credential, or network
    failure falls back fully to SHVYA's recorded send and webhook statuses.
    """

    ids = _validate_request(
        template_ids=template_ids,
        start_date=start_date,
        end_date=end_date,
    )
    if not ids:
        return {}

    local_results = fetch_local_template_analytics(
        account=account,
        template_ids=ids,
        start_date=start_date,
        end_date=end_date,
    )
    try:
        if not account.waba_id or not account.access_token:
            raise TemplateAnalyticsError(
                "This WhatsApp business is missing the Meta credentials required for analytics."
            )
        meta_results = _fetch_meta_template_analytics(
            account=account,
            template_ids=ids,
            start_date=start_date,
            end_date=end_date,
        )
        return _merge_local_receipt_floor(
            meta_results=meta_results,
            local_results=local_results,
        )
    except TemplateAnalyticsError as exc:
        for result in local_results.values():
            result["provider_warning"] = str(exc)
            result["provider_error_code"] = exc.meta_error_code
        return local_results
