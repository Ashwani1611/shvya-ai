from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from services.channels.campaign_policy import CampaignInputError, render_message
from services.channels.template_media import media_defaults, remap_carousel_delivery_media, resolve_delivery_media
from services.channels.template_rendering import render_for_lead
from services.channels.template_service import TemplateError, _clean_carousel_config, _merge_carousel_handles


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
class TemplateMediaDeliveryTests(SimpleTestCase):
    def setUp(self):
        catalog = patch("services.channels.template_rendering.available_placeholders", return_value=[
            {"key": key} for key in ("name", "lead_name", "lead_first_name", "phone", "email", "org_name", "lead_source", "user_name", "pipeline_name", "stage_name")
        ])
        catalog.start()
        self.addCleanup(catalog.stop)
        self.state = SimpleNamespace(delivery_media={
            "header.media": {"asset": "abc", "path": "private/image", "kind": "image", "name": "image.png", "mime": "image/png"},
        }, components=[{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Hello {{name}}"}], placeholder_mapping={})
        self.template = SimpleNamespace(organization_id="org", account_id="account", buttons=[])
        self.lead = SimpleNamespace(name="Asha", phone="+919876543210", email="", organization=SimpleNamespace(name="Test"), pipeline_id=None, stage_id=None, attributes={"name": "Asha"})

    def test_approval_handle_is_not_a_delivery_id(self):
        self.state.delivery_media = {}
        components = [{"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::opaque-sample"]}}]
        self.assertEqual(media_defaults(self.state, components), {})
        components[0]["example"]["header_handle"] = ["https://example.com/image.png"]
        self.assertEqual(media_defaults(self.state, components), {"header.media": "https://example.com/image.png"})

    def test_uploaded_media_and_named_body_render_for_inbox_and_sequences(self):
        with patch("services.channels.template_rendering.state_for", return_value=self.state):
            result = render_for_lead(template=self.template, lead=self.lead)
        self.assertEqual(result["components"], [
            {"type": "header", "parameters": [{"type": "image", "image": {"shvya_asset": "abc"}}]},
            {"type": "body", "parameters": [{"type": "text", "text": "Asha", "parameter_name": "name"}]},
        ])
        self.assertEqual(result["body_text"], "Hello Asha")

    def test_campaign_uses_attachment_without_per_recipient_fallback(self):
        spec = {"components": self.state.components, "media_defaults": media_defaults(self.state, self.state.components)}
        result = render_message(spec, {"body.name": {"source": "name"}}, {"name": "Asha"}, {"name"})
        self.assertEqual(result["components"][0]["parameters"][0]["image"], {"shvya_asset": "abc"})
        with self.assertRaisesRegex(CampaignInputError, "does not belong"):
            render_message(spec, {"header.media": {"default": "asset:foreign"}}, {}, set())

    def test_worker_uploads_file_and_does_not_mutate_frozen_components(self):
        client = Mock()
        client.upload_media.return_value = {"id": "123456"}
        components = [{"type": "header", "parameters": [{"type": "image", "image": {"shvya_asset": "abc"}}]}]
        with patch("services.channels.template_service.state_for", return_value=self.state), patch("services.channels.template_media.default_storage.open", return_value=BytesIO(b"image")):
            result = resolve_delivery_media(components=components, template=self.template, client=client)
        self.assertEqual(result[0]["parameters"][0]["image"], {"id": "123456"})
        self.assertEqual(components[0]["parameters"][0]["image"], {"shvya_asset": "abc"})
        client.upload_media.assert_called_once()

    def test_wrong_asset_is_rejected_before_storage_or_provider_access(self):
        client = Mock()
        components = [{"type": "header", "parameters": [{"type": "image", "image": {"shvya_asset": "foreign"}}]}]
        with patch("services.channels.template_service.state_for", return_value=self.state), patch("services.channels.template_media.default_storage.open") as storage:
            with self.assertRaises(CampaignInputError):
                resolve_delivery_media(components=components, template=self.template, client=client)
        storage.assert_not_called()
        client.upload_media.assert_not_called()

    def test_plain_template_has_no_header_parameter(self):
        self.state.components = [{"type": "BODY", "text": "Hello"}]
        with patch("services.channels.template_rendering.state_for", return_value=self.state):
            result = render_for_lead(template=self.template, lead=self.lead)
        self.assertEqual(result, {"body": "Hello", "body_text": "Hello", "components": []})

    def test_positional_mapping_renders_exact_body_without_footer_or_header(self):
        self.state.placeholder_mapping = {"1": "lead_name"}
        self.state.components = [
            {"type": "HEADER", "format": "TEXT", "text": "Offer"},
            {"type": "BODY", "text": "Welcome {{1}}"},
            {"type": "FOOTER", "text": "Terms apply"},
        ]
        self.lead.attributes["lead_name"] = "Untrusted shadow value"
        with patch("services.channels.template_rendering.state_for", return_value=self.state):
            result = render_for_lead(template=self.template, lead=self.lead)
        self.assertEqual(result["body_text"], "Welcome Asha")
        self.assertEqual(result["body"], "Offer\n\nWelcome Asha\n\nTerms apply")
        self.assertEqual(result["components"], [{"type": "body", "parameters": [{"type": "text", "text": "Asha"}]}])

    def test_unmapped_positional_parameter_never_uses_meta_sample_customer(self):
        self.state.components = [{"type": "BODY", "text": "Hello {{1}}", "example": {"body_text": [["Someone else"]]}}]
        with patch("services.channels.template_rendering.state_for", return_value=self.state):
            with self.assertRaisesRegex(CampaignInputError, "Missing value"):
                render_for_lead(template=self.template, lead=self.lead)

    def test_legacy_definition_includes_coupon_button_and_footer(self):
        self.state.components = []
        self.template.organization = self.lead.organization
        self.template.body = "Hello"
        self.template.footer = "Terms apply"
        self.template.attachment_type = "none"
        self.template.buttons = [{"type": "copy_offer", "coupon_code": "SAVE10"}]
        with patch("services.channels.template_rendering.state_for", return_value=self.state), patch("services.channels.template_rendering.build_meta_body", return_value=("Hello", {})):
            result = render_for_lead(template=self.template, lead=self.lead)
        self.assertEqual(result["body_text"], "Hello")
        self.assertEqual(result["components"], [{"type": "button", "sub_type": "copy_code", "index": "0", "parameters": [{"type": "coupon_code", "coupon_code": "SAVE10"}]}])

    def test_missing_carousel_definition_blocks_instead_of_sending_body_only(self):
        self.state.components = []
        self.template.template_format = "carousel"
        with patch("services.channels.template_rendering.state_for", return_value=self.state):
            with self.assertRaisesRegex(CampaignInputError, "Sync templates"):
                render_for_lead(template=self.template, lead=self.lead)

    def test_each_carousel_card_resolves_its_own_media(self):
        self.state.delivery_media = {"card0.header.media": self.state.delivery_media["header.media"]}
        components = [{"type": "carousel", "cards": [{"card_index": 0, "components": [{"type": "header", "parameters": [{"type": "image", "image": {"shvya_asset": "abc"}}]}]}]}]
        client = Mock()
        client.upload_media.return_value = {"id": "123456"}
        with patch("services.channels.template_service.state_for", return_value=self.state), patch("services.channels.template_media.default_storage.open", return_value=BytesIO(b"image")):
            result = resolve_delivery_media(components=components, template=self.template, client=client)
        self.assertEqual(result[0]["cards"][0]["components"][0]["parameters"][0]["image"], {"id": "123456"})


class CarouselMediaIdentityTests(SimpleTestCase):
    def test_reorder_keeps_each_file_with_its_original_card(self):
        first = {"uid": "first", "media_type": "image"}
        second = {"uid": "second", "media_type": "image"}
        stored = {"card0.header.media": {"asset": "first-file", "kind": "image"}, "card1.header.media": {"asset": "second-file", "kind": "image"}}
        result = remap_carousel_delivery_media(stored, {"cards": [first, second]}, {"cards": [second, first]})
        self.assertEqual(result["card0.header.media"]["asset"], "second-file")
        self.assertEqual(result["card1.header.media"]["asset"], "first-file")
        self.assertEqual(stored["card0.header.media"]["asset"], "first-file")

    def test_new_removed_and_changed_format_cards_never_reuse_another_file(self):
        old = {"cards": [{"uid": "first", "media_type": "image"}, {"uid": "removed", "media_type": "image"}]}
        new = {"cards": [{"uid": "new", "media_type": "image"}, {"uid": "first", "media_type": "video"}]}
        stored = {"card0.header.media": {"asset": "first-file", "kind": "image"}, "card1.header.media": {"asset": "removed-file", "kind": "image"}}
        self.assertEqual(remap_carousel_delivery_media(stored, old, new), {})

    def test_untrusted_or_other_account_card_sample_handles_are_cleared(self):
        card = {"uid": "first", "media_type": "image", "header_handle": "other-account", "media_name": "old.png", "mime_type": "image/png", "file_size": 50}
        result = _merge_carousel_handles({"cards": [card]}, {})
        self.assertEqual(result["cards"][0]["header_handle"], "")
        self.assertEqual(result["cards"][0]["media_name"], "")

    def test_duplicate_card_identity_is_rejected(self):
        with self.assertRaisesRegex(TemplateError, "unique identity"):
            _clean_carousel_config({"cards": [{"uid": "same"}, {"uid": "same"}]})
