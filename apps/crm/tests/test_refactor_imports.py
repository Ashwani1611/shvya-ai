from django.test import SimpleTestCase


class ViewImportsTest(SimpleTestCase):
    def test_all_views_importable(self):
        from apps.crm.views import (
            crm_login_view, crm_profile_view, dashboard_view,
            lead_table_partial, lead_edit_modal, lead_edit_stages,
            lead_edit_save, lead_filters_modal, lead_filters_values,
            LeadUpsertAPIView, LeadListAPIView, BulkMoveStageAPIView,
        )
        self.assertTrue(callable(crm_login_view))
        self.assertTrue(callable(dashboard_view))
