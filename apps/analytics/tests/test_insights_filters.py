from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase, RequestFactory

from apps.analytics.views.web import _grouped_series, _parse_pipeline_ids, _parse_date_range
from services.analytics.analytics_service import _date_scope


class InsightsFilterTests(SimpleTestCase):
    def test_day_bounds_follow_organization_timezone(self):
        qs = Mock()
        qs.filter.return_value = qs
        _date_scope(qs, organization=SimpleNamespace(timezone="Asia/Kolkata"),
                    date_from="2026-01-02", date_to="2026-01-02")
        bounds = [list(call.kwargs.values())[0].astimezone(dt_timezone.utc) for call in qs.filter.call_args_list]
        self.assertEqual(bounds, [datetime(2026, 1, 1, 18, 30, tzinfo=dt_timezone.utc),
                                  datetime(2026, 1, 2, 18, 30, tzinfo=dt_timezone.utc)])

    def test_dst_day_has_23_hours(self):
        qs = Mock()
        qs.filter.return_value = qs
        _date_scope(qs, organization=SimpleNamespace(timezone="America/New_York"),
                    date_from="2026-03-08", date_to="2026-03-08")
        bounds = [list(call.kwargs.values())[0].astimezone(dt_timezone.utc) for call in qs.filter.call_args_list]
        self.assertEqual((bounds[1] - bounds[0]).total_seconds(), 23 * 3600)

    def test_series_zero_fills_dates_and_sums_duplicate_groups(self):
        labels, series = _grouped_series([
            {"day": date(2026, 1, 1), "source": "manual", "count": 2},
            {"day": date(2026, 1, 1), "source": "manual", "count": 3},
        ], date_from="2026-01-01", date_to="2026-01-03", group_key="source")
        self.assertEqual(labels, ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(series[0]["values"], [5, 0, 0])

    def test_invalid_pipeline_id_is_not_passed_to_uuid_query(self):
        request = RequestFactory().get("/", {"pipeline": ["invalid", "00000000-0000-0000-0000-000000000001"]})
        self.assertEqual(_parse_pipeline_ids(request), ["00000000-0000-0000-0000-000000000001"])

    def test_reversed_dates_are_normalized(self):
        request = RequestFactory().get("/", {"date_from": "2026-02-02", "date_to": "2026-02-01"})
        request.crm_user = SimpleNamespace(organization=SimpleNamespace(timezone="Asia/Kolkata"))
        self.assertEqual(_parse_date_range(request), ("2026-02-01", "2026-02-02"))
