import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.integrations.models import MetaConversionDelivery as Delivery
from apps.integrations.models import MetaConversionMapping as Mapping
from apps.integrations.models import MetaConversionsConfiguration
from apps.integrations.services.meta_conversions import (
    build_event, build_user_data, capture_stage_event, dashboard_state, get_configuration,
    perform_action, queue_event, save_mapping, save_settings,
)
from apps.integrations.services.meta_conversions_delivery import deliver_event, recover_due_events
from apps.organizations.models import Organization
from services.crm.lead_service import upsert_lead
from services.crm.lead_transition import move_lead_to_stage


class MetaConversionsMatchingTests(SimpleTestCase):
    def test_meta_documented_email_and_phone_hash_examples(self):
        configuration = MetaConversionsConfiguration(user_data_fields=["email", "phone"])
        lead = Lead(email="  John_Smith@gmail.com  ", phone="+1 (650) 555-1212")
        data = build_user_data(configuration, lead)
        self.assertEqual(data["em"], ["62a14e44f765419d10fea99367361a727c12365e2520f32218d505ed9aa0f62f"])
        self.assertEqual(data["ph"], ["e323ec626319ca94ee8bff2e4c87cf613be6ea19919ed1364124e16807ab3176"])

    def test_meta_documented_utf8_name_hash_examples(self):
        configuration = MetaConversionsConfiguration(user_data_fields=["name", "external_id"])
        for name, expected in [
            ("Valéry", "08e1996b5dd49e62a4b4c010d44e4345592a863bb9f8e3976219bac29417149c"),
            ("정", "8fa8cd9c440be61d0151429310034083132b35975c4bea67fdd74158eb51db14"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(build_user_data(configuration, Lead(name=name))["fn"], [expected])

    def test_indic_customer_fields_preserve_unicode_combining_marks(self):
        configuration = MetaConversionsConfiguration(user_data_fields=["name", "city", "state", "external_id"])
        lead = Lead(name="गौरव सिंह", attributes={"city": "मुंबई", "state": "महाराष्ट्र"})
        data = build_user_data(configuration, lead)
        for field, normalized in [("fn", "गौरव"), ("ln", "सिंह"), ("ct", "मुंबई"), ("st", "महाराष्ट्र")]:
            with self.subTest(field=field):
                self.assertEqual(data[field], [hashlib.sha256(normalized.encode("utf-8")).hexdigest()])

    def test_customer_country_gender_and_us_zip_are_trimmed_before_hashing(self):
        configuration = MetaConversionsConfiguration(user_data_fields=["country", "gender", "zip_code", "external_id"])
        lead = Lead(attributes={"country": " US ", "gender": " Female ", "zip_code": "94035-1234"})
        data = build_user_data(configuration, lead)
        for field, normalized in [("zp", "94035"), ("country", "us"), ("ge", "f")]:
            with self.subTest(field=field):
                self.assertEqual(data[field], [hashlib.sha256(normalized.encode()).hexdigest()])

    def test_meta_baseline_matching_rejects_broad_identifier_combinations(self):
        configuration = MetaConversionsConfiguration()
        cases = [
            (["city", "country", "state", "zip_code", "gender", "user_agent"], "Gaurav Singh",
             {"city": "Mumbai", "country": "in", "state": "Maharashtra", "zip_code": "400001", "gender": "male", "user_agent": "Mozilla/5.0"}),
            (["date_of_birth", "user_agent"], "Gaurav Singh",
             {"date_of_birth": "1997-02-16", "user_agent": "Mozilla/5.0"}),
            (["name", "gender"], "Gaurav", {"gender": "male"}),
            (["name", "gender"], ". Singh", {"gender": "male"}),
        ]
        for fields, name, attributes in cases:
            with self.subTest(fields=fields, name=name):
                lead = Lead(name=name, attributes=attributes)
                configuration.user_data_fields = fields
                with self.assertRaisesMessage(ValidationError, "matching requirements"):
                    build_user_data(configuration, lead)
                configuration.user_data_fields = [*fields, "external_id"]
                self.assertIn("external_id", build_user_data(configuration, lead))


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class MetaConversionsTests(TestCase):
    def setUp(self):
        self.publisher = patch("apps.integrations.tasks.deliver_meta_conversion_task.delay").start()
        self.addCleanup(patch.stopall)
        self.org = Organization.objects.create(name="Meta Conversions Test")
        self.other = Organization.objects.create(name="Other Conversions Tenant")
        self.pipeline = Pipeline.objects.filter(organization=self.org, is_active=True).first()
        self.stage = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.configuration = get_configuration(self.org)
        self.configuration.dataset_id = "123456789012345"
        self.configuration.test_event_code = "TEST12345"
        self.configuration.set_access_token("test-only-meta-access-token")
        self.configuration.save()
        self.mapping = self.configuration.mappings.get(stage=self.stage)
        self.lead = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Gaurav Singh", phone="+919876543210", email="Gaurav@example.com",
            lead_source="meta_ads", attributes={"meta_leadgen_id": "1234567890123456"},
        )
        self.url = reverse("crm-connect-hub-meta-conversions-api")

    def enable(self, *, test_mode=False, event_scope="meta_leads"):
        self.configuration.is_enabled = True
        self.configuration.test_mode = test_mode
        self.configuration.event_scope = event_scope
        self.configuration.save()

    def settings_data(self, **changes):
        return {
            "dataset_id": self.configuration.dataset_id, "access_token": "",
            "is_enabled": False, "test_mode": True, "test_event_code": "TEST12345",
            "event_scope": "meta_leads", "user_data_fields": ["email", "phone", "lead_id", "external_id"],
            "custom_attribute_keys": [], **changes,
        }

    def mapping_data(self, mapping=None, **changes):
        return {
            "id": str(mapping.pk) if mapping else "", "pipeline": str(self.pipeline.pk),
            "stage": str(self.qualified.pk), "event_name": "QualifiedLead",
            "is_enabled": True, "action_source": "system_generated", "value_source": "none",
            "static_value": "", "value_attribute": "", "currency": "", **changes,
        }

    def delivery(self, **kwargs):
        return queue_event(self.configuration, self.mapping, self.lead, occurred_at=timezone.now(), **kwargs)

    def authenticate(self, role="admin", client=None):
        user = User.objects.create_user(
            email=f"{role}@capi.example", password="test-only-password", name="Test User",
            organization=self.org, role=role,
        )
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        (client or self.client).cookies[get_session_cookie_name("dashboard")] = session.session_key

    def test_connection_token_encrypted_and_never_returned(self):
        encrypted = self.configuration.encrypted_access_token
        self.assertNotIn("test-only-meta-access-token", encrypted)
        saved = save_settings(self.configuration, self.settings_data())
        self.assertEqual(saved.encrypted_access_token, encrypted)
        self.assertEqual(saved.get_access_token(), "test-only-meta-access-token")
        state = json.dumps(dashboard_state(saved))
        self.assertNotIn("test-only-meta-access-token", state)
        self.assertNotIn(encrypted, state)

    def test_mappings_enforce_pipeline_stage_and_tenant(self):
        other_pipeline = Pipeline.objects.filter(organization=self.other).first()
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data(pipeline=str(other_pipeline.pk)))
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data(stage=str(other_pipeline.stages.first().pk)))
        mapping = save_mapping(self.configuration, self.mapping_data())
        self.assertEqual(mapping.event_name, "QualifiedLead")
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data())

    def test_default_mapping_is_locked_and_can_be_paused(self):
        with self.assertRaises(ValidationError):
            perform_action(self.configuration, {"action": "delete_mapping", "id": str(self.mapping.pk)})
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data(self.mapping, stage=str(self.stage.pk), event_name="Purchase"))
        saved = save_mapping(self.configuration, self.mapping_data(self.mapping,
            stage=str(self.stage.pk), event_name="Lead", is_enabled=False))
        self.assertFalse(saved.is_enabled)

    def test_payload_has_crm_metadata_hashed_contact_and_real_meta_id(self):
        event = build_event(self.configuration, self.mapping, self.lead, event_id="stable-event", occurred_at=timezone.now())
        def digest(text):
            return hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(event["user_data"]["em"], [digest("gaurav@example.com")])
        self.assertEqual(event["user_data"]["ph"], [digest("919876543210")])
        self.assertEqual(event["user_data"]["fn"], [digest("gaurav")])
        self.assertEqual(event["user_data"]["lead_id"], "1234567890123456")
        self.assertNotEqual(event["user_data"]["lead_id"], str(self.lead.pk))
        self.assertEqual(event["custom_data"], {"event_source": "crm", "lead_event_source": "SHVYA AI"})
        self.assertEqual(event["action_source"], "system_generated")
        self.assertIsInstance(event["event_time"], int)

    def test_static_and_attribute_value_currency_and_custom_data(self):
        AttributeDefinition.objects.create(organization=self.org, name="Deal amount", key="deal_amount", field_type="numeric")
        AttributeDefinition.objects.create(organization=self.org, name="Product", key="product")
        AttributeDefinition.objects.create(organization=self.org, name="Secret", key="access_token")
        self.configuration.custom_attribute_keys = ["product", "access_token", "email"]
        self.lead.attributes.update(deal_amount="1250.50", product="Gym membership", access_token="private-secret")
        self.mapping.value_source, self.mapping.static_value, self.mapping.currency = "static", Decimal("4500.50"), "INR"
        event = build_event(self.configuration, self.mapping, self.lead, event_id="value", occurred_at=timezone.now())
        self.assertEqual(event["custom_data"]["value"], 4500.5)
        self.assertEqual(event["custom_data"]["currency"], "INR")
        self.assertEqual(event["custom_data"]["product"], "Gym membership")
        self.assertNotIn("private-secret", json.dumps(event))
        self.mapping.value_source, self.mapping.value_attribute = "attribute", "deal_amount"
        self.assertEqual(build_event(self.configuration, self.mapping, self.lead, event_id="attr", occurred_at=timezone.now())["custom_data"]["value"], 1250.5)
        self.lead.attributes["deal_amount"] = "NaN"
        with self.assertRaises(ValidationError):
            build_event(self.configuration, self.mapping, self.lead, event_id="bad-value", occurred_at=timezone.now())

    def test_purchase_requires_value_and_numeric_attribute_owned_by_tenant(self):
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data(event_name="Purchase"))
        AttributeDefinition.objects.create(organization=self.other, name="Private amount", key="private_amount", field_type="numeric")
        with self.assertRaises(ValidationError):
            save_mapping(self.configuration, self.mapping_data(value_source="attribute", value_attribute="private_amount", currency="INR"))

    def test_creation_stage_change_and_new_acquisition_are_captured_once(self):
        self.enable()
        save_mapping(self.configuration, self.mapping_data())
        with self.captureOnCommitCallbacks(execute=True):
            lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
                name="Second lead", phone="+919876543211", lead_source="meta_ads",
                attributes={"meta_leadgen_id": "1234567890123457"})
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 1)
        self.publisher.assert_called_once()
        lead.name = "Renamed lead"
        lead.save(update_fields=["name", "updated_at"])
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 1)
        move_lead_to_stage(lead=lead, stage=self.qualified)
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 2)
        lead.save()
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 2)
        lead.attributes["meta_leadgen_id"] = "1234567890123458"
        lead.save(update_fields=["attributes", "updated_at"])
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 3)

    def test_rollback_does_not_publish_or_keep_event(self):
        self.enable()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.lead.attributes["meta_leadgen_id"] = "1234567890123499"
                    self.lead.save()
                    raise RuntimeError("test rollback")
            except RuntimeError:
                pass
        self.assertFalse(Delivery.objects.exists())
        self.publisher.assert_not_called()

    def test_stage_saved_by_foreign_key_attname_is_captured(self):
        self.enable()
        save_mapping(self.configuration, self.mapping_data())
        self.lead.stage_id = self.qualified.pk
        self.lead.save(update_fields=["stage_id", "updated_at"])
        self.assertEqual(Delivery.objects.get().payload["event_name"], "QualifiedLead")

    def test_partial_attribute_save_does_not_capture_an_unsaved_stage(self):
        self.enable()
        save_mapping(self.configuration, self.mapping_data())
        self.lead.stage = self.qualified
        self.lead.attributes["product"] = "Membership"
        self.lead.save(update_fields=["attributes", "updated_at"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.stage.pk)
        self.assertFalse(Delivery.objects.exists())

    def test_stage_event_uses_persisted_customer_data_after_partial_save(self):
        self.enable()
        save_mapping(self.configuration, self.mapping_data())
        original_email = self.lead.email
        self.lead.attributes["meta_leadgen_id"] = "1234567890123499"
        self.lead.email = "unsaved@example.com"
        self.lead.stage = self.qualified
        self.lead.save(update_fields=["stage", "updated_at"])
        persisted = Lead.objects.get(pk=self.lead.pk, organization=self.org)
        user_data = Delivery.objects.get().payload["user_data"]
        # Qualification state deliberately persists attributes on stage moves.
        # The contact email is excluded from that update and must stay unsaved.
        self.assertEqual(persisted.attributes["meta_leadgen_id"], "1234567890123499")
        self.assertEqual(user_data["lead_id"], persisted.attributes["meta_leadgen_id"])
        self.assertEqual(persisted.email, original_email)
        self.assertEqual(user_data["em"], [hashlib.sha256(original_email.lower().encode()).hexdigest()])

    def test_meta_form_reacquisition_of_an_existing_contact_is_captured(self):
        existing = Lead.objects.create(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Existing contact", phone="+919876543212", lead_source="system",
        )
        self.enable()
        lead, created = upsert_lead(
            organization=self.org, pipeline=self.pipeline, stage=self.stage,
            name="Returning Meta contact", phone=existing.phone, lead_source="meta_ads",
            attributes={"meta_leadgen_id": "1234567890123498"},
        )
        self.assertFalse(created)
        self.assertEqual(lead.pk, existing.pk)
        self.assertEqual(lead.lead_source, "system")
        event = Delivery.objects.get(lead=lead).payload
        self.assertEqual(event["user_data"]["lead_id"], "1234567890123498")
        self.assertEqual(event["action_source"], "system_generated")
        self.assertEqual(event["custom_data"]["event_source"], "crm")
        self.authenticate()
        response = self.client.get(self.url, {"lookup": "leads", "mapping_id": str(self.mapping.pk)})
        self.assertIn(str(lead.pk), {item["id"] for item in response.json()["leads"]})
        lead.save()
        self.assertEqual(Delivery.objects.filter(lead=lead).count(), 1)

    def test_invalid_matching_records_an_actionable_failure_without_publication(self):
        self.configuration.user_data_fields = ["country"]
        self.lead.attributes["country"] = "in"
        with self.captureOnCommitCallbacks(execute=True):
            delivery = self.delivery()
        self.assertEqual(delivery.status, Delivery.Status.FAILED)
        self.assertIn("matching requirements", delivery.error_message)
        self.publisher.assert_not_called()

    def test_disabled_tracking_mapping_and_non_meta_scope_do_not_emit(self):
        capture_stage_event(self.configuration, self.lead)
        self.assertFalse(Delivery.objects.exists())
        self.enable()
        self.mapping.is_enabled = False
        self.mapping.save()
        capture_stage_event(self.configuration, self.lead)
        self.assertFalse(Delivery.objects.exists())
        self.mapping.is_enabled = True
        self.mapping.save()
        self.lead.lead_source = "whatsapp"
        self.lead.attributes = {}
        capture_stage_event(self.configuration, self.lead)
        self.assertFalse(Delivery.objects.exists())
        self.configuration.event_scope = "all_leads"
        self.configuration.save()
        self.mapping.action_source = "chat"
        self.mapping.save()
        capture_stage_event(self.configuration, self.lead)
        self.assertEqual(Delivery.objects.get().payload["action_source"], "chat")
        self.assertNotIn("custom_data", Delivery.objects.get().payload)

    def test_browser_and_click_identifiers_are_not_fabricated_or_hashed(self):
        self.configuration.user_data_fields = ["fbp", "fbc", "ip_address", "user_agent", "wa_ref_ctwa_clid"]
        self.lead.attributes.update(fbp="fb.1.1596403881668.1116446470", fbc="fb.1.1554763741205.AbCdEf",
            ip_address="2001:db8::1", user_agent="Mozilla/5.0", wa_ref_ctwa_clid="real-click-id")
        data = build_event(self.configuration, self.mapping, self.lead, event_id="browser", occurred_at=timezone.now())["user_data"]
        self.assertEqual(data["fbp"], self.lead.attributes["fbp"])
        self.assertEqual(data["ctwa_clid"], "real-click-id")
        self.assertEqual(data["client_ip_address"], "2001:db8::1")
        self.lead.attributes = {}
        with self.assertRaises(ValidationError):
            build_event(self.configuration, self.mapping, self.lead, event_id="empty", occurred_at=timezone.now())

    def test_website_events_need_real_url_and_browser(self):
        self.lead.lead_source = "external_api"
        self.lead.attributes = {}
        self.mapping.action_source = "website"
        with self.assertRaises(ValidationError):
            build_event(self.configuration, self.mapping, self.lead, event_id="web", occurred_at=timezone.now())
        self.configuration.user_data_fields.append("user_agent")
        self.lead.attributes.update(event_source_url="https://example.com/contact", user_agent="Mozilla/5.0")
        self.assertEqual(build_event(self.configuration, self.mapping, self.lead, event_id="web", occurred_at=timezone.now())["event_source_url"], "https://example.com/contact")

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_delivery_posts_official_shape_and_counts_only_accepted_events(self, post):
        self.enable()
        delivery = self.delivery(event_id="stable-event")
        post.return_value = Mock(status_code=200, json=Mock(return_value={"events_received": 1, "fbtrace_id": "trace123"}))
        self.assertEqual(deliver_event(delivery.pk)["status"], "sent")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://graph.facebook.com/v26.0/123456789012345/events")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-only-meta-access-token")
        self.assertEqual(kwargs["json"], {"data": [delivery.payload]})
        self.assertNotIn("test_event_code", kwargs["json"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(deliver_event(delivery.pk)["status"], "sent")
        post.assert_called_once()
        self.configuration.refresh_from_db()
        self.assertIsNotNone(self.configuration.verified_at)

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_transient_retry_keeps_original_event_payload_and_is_bounded(self, post):
        self.enable()
        delivery = self.delivery()
        snapshot = delivery.payload.copy()
        post.side_effect = requests.Timeout("unsafe-provider-error-must-not-be-saved")
        self.assertEqual(deliver_event(delivery.pk)["status"], "retrying")
        delivery.refresh_from_db()
        self.assertEqual(delivery.payload, snapshot)
        self.assertIsNone(delivery.lease_until)
        self.assertNotIn("unsafe-provider-error", delivery.error_message)
        Delivery.objects.filter(pk=delivery.pk).update(attempt_count=5, next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(deliver_event(delivery.pk)["status"], "failed")
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempt_count, 6)
        self.assertEqual(delivery.payload, snapshot)

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_permanent_meta_errors_are_redacted_and_not_retried(self, post):
        self.enable()
        delivery = self.delivery()
        post.return_value = Mock(status_code=400, json=Mock(return_value={"error": {
            "code": 190, "message": "test-only-meta-access-token private-customer-value",
            "is_transient": True, "fbtrace_id": "errortrace",
        }}))
        self.assertEqual(deliver_event(delivery.pk)["status"], "failed")
        delivery.refresh_from_db()
        self.assertEqual(delivery.meta_error_code, 190)
        self.assertNotIn("test-only-meta-access-token", delivery.error_message)
        self.assertNotIn("private-customer", json.dumps(dashboard_state(self.configuration)))
        deliver_event(delivery.pk)
        post.assert_called_once()

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_partial_acceptance_is_failure_and_live_test_code_never_leaks(self, post):
        self.enable()
        delivery = self.delivery()
        post.return_value = Mock(status_code=200, json=Mock(return_value={"events_received": 0}))
        self.assertEqual(deliver_event(delivery.pk)["status"], "failed")
        self.assertNotIn("test_event_code", post.call_args.kwargs["json"])

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_manual_probe_can_send_while_tracking_is_paused(self, post):
        delivery = self.delivery(is_test=True, is_probe=True)
        post.return_value = Mock(status_code=200, json=Mock(return_value={"events_received": 1}))
        self.assertEqual(deliver_event(delivery.pk)["status"], "sent")
        self.assertEqual(post.call_args.kwargs["json"]["test_event_code"], "TEST12345")
        automatic = self.delivery(is_test=True)
        self.assertEqual(deliver_event(automatic.pk)["status"], "paused")
        post.assert_called_once()

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_leases_prevent_concurrent_calls_and_recovery_finds_abandoned_events(self, post):
        self.enable()
        delivery = self.delivery()
        Delivery.objects.filter(pk=delivery.pk).update(status="sending", lease_until=timezone.now() + timedelta(seconds=60))
        self.assertEqual(deliver_event(delivery.pk)["status"], "busy")
        post.assert_not_called()
        Delivery.objects.filter(pk=delivery.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(recover_due_events()["queued"], 1)
        self.publisher.assert_called_once_with(str(delivery.pk))

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_expired_events_and_changed_dataset_never_post(self, post):
        self.enable()
        expired = self.delivery()
        payload = expired.payload.copy()
        payload["event_time"] = int((timezone.now() - timedelta(days=8)).timestamp())
        Delivery.objects.filter(pk=expired.pk).update(payload=payload)
        self.assertEqual(deliver_event(expired.pk)["status"], "failed")
        pending = self.delivery()
        save_settings(self.configuration, self.settings_data(dataset_id="999999999999999", access_token="new-test-only-token", is_enabled=True, test_mode=False))
        self.assertEqual(deliver_event(pending.pk)["status"], "skipped")
        post.assert_not_called()

    def test_durable_capture_survives_broker_outage_and_retry_is_idempotent(self):
        self.enable()
        self.publisher.side_effect = RuntimeError("test broker unavailable")
        with self.captureOnCommitCallbacks(execute=True):
            occurred_at = timezone.now()
            capture_stage_event(self.configuration, self.lead, occurred_at=occurred_at)
            capture_stage_event(self.configuration, self.lead, occurred_at=occurred_at)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(Delivery.objects.get().status, "queued")

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_removed_mapping_recovers_abandoned_delivery_without_sending(self, post):
        self.enable()
        mapping = save_mapping(self.configuration, self.mapping_data())
        delivery = queue_event(self.configuration, mapping, self.lead, occurred_at=timezone.now())
        Delivery.objects.filter(pk=delivery.pk).update(
            status="sending", lease_until=timezone.now() - timedelta(seconds=1),
        )
        perform_action(self.configuration, {"action": "delete_mapping", "id": str(mapping.pk)})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(recover_due_events()["queued"], 1)
        self.assertEqual(deliver_event(delivery.pk)["status"], "skipped")
        post.assert_not_called()

    def test_retry_reuses_event_identity_and_requires_active_mapping(self):
        self.enable()
        delivery = self.delivery(event_id="original-identity")
        original_payload = delivery.payload.copy()
        Delivery.objects.filter(pk=delivery.pk).update(status="failed", attempt_count=6)
        self.mapping.is_enabled = False
        self.mapping.save()
        with self.assertRaises(ValidationError):
            perform_action(self.configuration, {"action": "retry_delivery", "id": str(delivery.pk)})
        self.mapping.is_enabled = True
        self.mapping.save()
        perform_action(self.configuration, {"action": "retry_delivery", "id": str(delivery.pk)})
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(delivery.event_id, "original-identity")
        self.assertEqual(delivery.payload, original_payload)
        self.assertEqual(delivery.attempt_count, 0)

    def test_manual_retry_checks_the_original_event_timestamp(self):
        self.enable()
        delivery = self.delivery()
        payload = delivery.payload.copy()
        payload["event_time"] = int((timezone.now() - timedelta(days=8)).timestamp())
        Delivery.objects.filter(pk=delivery.pk).update(payload=payload, status="failed")
        with self.assertRaisesMessage(ValidationError, "seven-day delivery window"):
            perform_action(self.configuration, {"action": "retry_delivery", "id": str(delivery.pk)})
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, Delivery.Status.FAILED)
        self.assertEqual(delivery.payload, payload)

    @patch("apps.integrations.services.meta_conversions_delivery.requests.post")
    def test_lead_deletion_removes_conversion_payload_and_pending_delivery(self, post):
        self.enable()
        delivery = self.delivery()
        delivery_id = delivery.pk
        self.lead.delete()
        self.assertFalse(Delivery.objects.filter(pk=delivery_id).exists())
        self.assertEqual(deliver_event(delivery_id)["status"], "missing")
        post.assert_not_called()

    def test_page_and_json_are_admin_only_and_tenant_scoped(self):
        self.authenticate("agent")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, data=json.dumps({"action": "disconnect"}), content_type="application/json").status_code, 403)
        self.client.cookies.clear()
        self.authenticate("admin")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your stages. Meta")
        self.assertNotContains(response, "test-only-meta-access-token")
        foreign_mapping = get_configuration(self.other).mappings.first()
        result = self.client.post(self.url, data=json.dumps({"action": "delete_mapping", "id": str(foreign_mapping.pk)}), content_type="application/json")
        self.assertEqual(result.status_code, 400)
        self.assertTrue(Mapping.objects.filter(pk=foreign_mapping.pk).exists())
        self.assertEqual(self.client.get(self.url + "?format=json").headers["Cache-Control"], "no-store")

    def test_manual_tests_require_owned_lead_in_current_mapped_stage(self):
        other_pipeline = Pipeline.objects.filter(organization=self.other).first()
        foreign_lead = Lead.objects.create(
            organization=self.other, pipeline=other_pipeline, stage=other_pipeline.stages.first(),
            name="Foreign tenant lead", phone="+919876540000", lead_source="meta_ads",
            attributes={"meta_leadgen_id": "1234567890123466"},
        )
        with self.assertRaises(ValidationError):
            perform_action(self.configuration, {"action": "test_event",
                "mapping_id": str(self.mapping.pk), "lead_id": str(foreign_lead.pk)})
        qualified_mapping = save_mapping(self.configuration, self.mapping_data())
        with self.assertRaises(ValidationError):
            perform_action(self.configuration, {"action": "test_event",
                "mapping_id": str(qualified_mapping.pk), "lead_id": str(self.lead.pk)})
        self.assertFalse(Delivery.objects.exists())
        self.authenticate()
        response = self.client.get(self.url, {"lookup": "leads", "mapping_id": str(self.mapping.pk)})
        self.assertEqual(response.json()["leads"], [{"id": str(self.lead.pk), "name": self.lead.name}])
        perform_action(self.configuration, {"action": "test_event",
            "mapping_id": str(self.mapping.pk), "lead_id": str(self.lead.pk)})
        self.assertTrue(Delivery.objects.get().is_probe)
        self.assertTrue(Delivery.objects.get().is_test)

    def test_browser_mutations_require_csrf_and_malformed_json_is_rejected(self):
        strict_client = Client(enforce_csrf_checks=True)
        self.authenticate(client=strict_client)
        strict_client.get(self.url)
        self.assertEqual(strict_client.post(self.url, data=json.dumps({"action": "disconnect"}), content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.cookies.update(strict_client.cookies)
        self.assertEqual(self.client.post(self.url, data="{bad", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(self.url, data="[]", content_type="application/json").status_code, 400)
