"""Preview routing regressions with database lookups mocked; no provider I/O."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from services.channels.campaign_audience import campaign_account_for_pipeline
from services.channels.campaign_service import preview_campaign


class CampaignSenderPreviewTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(organization_id="org")
        self.account = SimpleNamespace(pk="sender")
        self.template = SimpleNamespace(account=self.account)
        self.pipeline = SimpleNamespace(pk="pipeline")
        self.upload = SimpleNamespace(
            review_digest="reviewed", review_config={},
            reviewed_rows=[{"row": 2, "name": "Asha", "existing_id": "", "pipeline_id": "pipeline", "values": {}}],
        )
        self.spec = {"components": [{"type": "BODY", "text": "Your requested update."}]}
        self.pipelines = Mock()
        self.pipelines.filter.return_value = [self.pipeline]
        for name, value in (
            ("get_template", self.template),
            ("template_snapshot", self.spec),
            ("source_catalog", []),
            ("user_pipelines", self.pipelines),
        ):
            mocked = patch(f"services.channels.campaign_service.{name}", return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def preview(self):
        return preview_campaign(user=self.user, upload=self.upload, template_id="template", bindings={})

    def test_multiple_recipients_on_one_linked_sender_require_only_one_route_lookup(self):
        self.upload.reviewed_rows *= 10
        with patch("services.channels.campaign_service.campaign_account_for_pipeline", return_value=self.account) as route:
            preview = self.preview()
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["recipient_count"], 10)
        self.assertEqual(len(preview["previews"]), 5)
        route.assert_called_once_with(user=self.user, pipeline=self.pipeline)


    def test_wrong_sender_and_missing_connection_are_actionable_preview_failures(self):
        for linked in (None, SimpleNamespace(pk="another_sender")):
            with self.subTest(linked=linked), patch("services.channels.campaign_service.campaign_account_for_pipeline", return_value=linked):
                preview = self.preview()
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["missing_count"], 1)
            self.assertEqual(preview["previews"], [])
            self.assertIn("pipeline", preview["errors"][0]["reason"])

    def test_existing_lead_current_pipeline_overrides_stale_audience_route(self):
        self.upload.reviewed_rows[0]["existing_id"] = "lead"
        reassigned = SimpleNamespace(pk="reassigned")
        self.pipelines.filter.return_value = [reassigned]
        with patch("services.channels.campaign_service.Lead.objects.filter") as leads, patch(
            "services.channels.campaign_service.campaign_account_for_pipeline", return_value=None,
        ) as route:
            leads.return_value.values_list.return_value = [("lead", "reassigned")]
            preview = self.preview()
        self.assertFalse(preview["ready"])
        self.pipelines.filter.assert_called_once_with(pk__in={"reassigned"})
        route.assert_called_once_with(user=self.user, pipeline=reassigned)
        leads.assert_called_once_with(organization_id="org", pk__in=["lead"])

    def test_explicit_reviewed_move_uses_destination_sender(self):
        self.upload.reviewed_rows[0]["existing_id"] = "lead"
        self.upload.review_config = {"update_existing": True, "move_existing": True}
        with patch("services.channels.campaign_service.Lead.objects.filter") as leads, patch(
            "services.channels.campaign_service.campaign_account_for_pipeline", return_value=self.account,
        ) as route:
            preview = self.preview()
        self.assertTrue(preview["ready"])
        leads.assert_not_called()
        route.assert_called_once_with(user=self.user, pipeline=self.pipeline)


class CampaignSenderBindingTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(organization_id="org", role="admin")
        self.pipeline = SimpleNamespace(
            organization_id="org", is_active=True, country_code="+91", phone_number="",
        )
        self.account = SimpleNamespace(phone_number_id="123456789012345678", display_phone_number="+919800000000")

    def resolve(self):
        query = Mock()
        query.exclude.return_value = query
        query.only.return_value = query
        query.order_by.return_value = [self.account]
        with patch("services.channels.campaign_audience.WhatsAppAccount.objects.filter", return_value=query) as accounts:
            result = campaign_account_for_pipeline(user=self.user, pipeline=self.pipeline)
        accounts.assert_called_once_with(organization_id="org", connection_type="api", status="connected", is_active=True)
        self.assertEqual([call.kwargs for call in query.exclude.call_args_list], [{"phone_number_id": ""}, {"access_token": ""}])
        query.order_by.assert_called_once_with("-connected_at", "-pk")
        return result

    def test_legacy_meta_phone_id_binding_does_not_require_valid_e164(self):
        self.pipeline.phone_number = self.account.phone_number_id
        self.assertIs(self.resolve(), self.account)

    def test_display_number_binding_normalizes_country_code_and_format(self):
        self.pipeline.phone_number = "98000 00000"
        self.assertIs(self.resolve(), self.account)

    def test_unknown_binding_never_falls_back_to_another_sender(self):
        self.pipeline.phone_number = "+919800000001"
        self.assertIsNone(self.resolve())

    def test_foreign_pipeline_is_blocked_before_account_lookup(self):
        self.pipeline.organization_id = "another-org"
        self.pipeline.phone_number = self.account.phone_number_id
        with patch("services.channels.campaign_audience.WhatsAppAccount.objects.filter") as accounts:
            self.assertIsNone(campaign_account_for_pipeline(user=self.user, pipeline=self.pipeline))
        accounts.assert_not_called()



class CampaignSavedBindingsTests(SimpleTestCase):
    def test_reuses_explicit_template_mapping_with_campaign_attribute_prefix(self):
        from services.channels.campaign_service import default_bindings
        spec = {"components": [{"type": "BODY", "text": "Hello {{1}}, {{2}}"}],
                "delivery_bindings": {"body.1": {"source": "lead_first_name", "default": "Customer"},
                                      "body.2": {"source": "company", "default": "your business"}}}
        result = default_bindings(spec, [{"key": "lead_first_name"}, {"key": "attr:company"}])
        self.assertEqual(result, {"body.1": {"source": "lead_first_name", "default": "Customer"},
                                  "body.2": {"source": "attr:company", "default": "your business"}})
