from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.core.api_activity.sanitization import (
    MAX_METADATA_BYTES, REDACTED, sanitize_endpoint, sanitize_metadata, sanitize_text,
)
from apps.core.api_activity.services import (
    activity_context, annotate_activity, bind_organization, capture_health, finish_activity,
    get_context, record_activity, record_event, start_activity,
)
from apps.core.api_activity.retention import prune_activities
from apps.core.models import APIActivity, APIActivityEvent
from apps.organizations.models import Organization


class APIActivitySanitizationTests(SimpleTestCase):
    def test_nested_credentials_payloads_and_camel_case_headers_are_redacted(self):
        safe = sanitize_metadata({
            "response": {"access_token": "private-token", "status": "accepted"},
            "requestHeaders": {"Authorization": "Bearer private-token"},
            "body": {"message": "private conversation"},
            "qr_data": "qr-secret", "session": "session-secret",
            "prompt": "private instructions", "phone": "+919876543210",
            "input_tokens": 42, "message_id": "123456789012345678",
            "message_status": "sent", "message_type": "text",
        })
        encoded = json.dumps(safe)
        for private in ("private-token", "private conversation", "qr-secret", "session-secret", "private instructions", "+919876543210"):
            self.assertNotIn(private, encoded)
        self.assertEqual(safe["input_tokens"], 42)
        self.assertEqual(safe["message_id"], "123456789012345678")
        self.assertEqual(safe["message_status"], "sent")

    def test_embedded_credentials_and_personal_data_are_removed(self):
        safe = sanitize_text(
            'Authorization: Bearer abcdefghijk password="private password" '
            'access_token=verysecret email person@example.com phone +919876543210 '
            'https://user:pass@example.com/a?token=supersecret#secret'
        )
        for secret in ("abcdefghijk", "private password", "verysecret", "person@example.com", "919876543210", "user:pass", "supersecret", "#secret"):
            self.assertNotIn(secret, safe)
        self.assertIn("https://example.com/a", safe)

    def test_endpoint_never_exposes_url_tokens(self):
        token = str(uuid.uuid4())
        result = sanitize_endpoint(f"https://user:password@example.com/webhooks/justdial/{token}/?access_token=secret")
        self.assertEqual(result, "https://example.com/webhooks/justdial/[redacted]/")
        self.assertEqual(
            sanitize_endpoint("https://example.com/api/justdial/sPX5AWNE/webhook"),
            "https://example.com/api/justdial/[redacted]/webhook",
        )

    def test_cyclic_and_oversized_metadata_is_bounded_json(self):
        cyclic = {"items": []}
        cyclic["items"].append(cyclic)
        result = sanitize_metadata(cyclic)
        self.assertIn("truncated", json.dumps(result))
        huge = {f"field_{i}": "x" * 9000 for i in range(1000)}
        self.assertLessEqual(len(json.dumps(sanitize_metadata(huge)).encode()), MAX_METADATA_BYTES)
        self.assertIsNone(sanitize_metadata({"duration": float("nan")})["duration"])

    def test_arbitrary_objects_are_not_stringified(self):
        class PrivateObject:
            def __str__(self):
                raise AssertionError("Must never inspect secret object")

        self.assertEqual(sanitize_metadata({"detail": PrivateObject()}), {"detail": "[unsupported]"})
        self.assertEqual(sanitize_text(PrivateObject()), "[unsupported]")


class APIActivityContextTests(SimpleTestCase):
    def test_context_is_restored_after_nested_error(self):
        outer = {"activity_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()), "organization_id": str(uuid.uuid4())}
        with activity_context(outer):
            original = get_context()
            with self.assertRaises(RuntimeError):
                with activity_context({"activity_id": str(uuid.uuid4())}):
                    self.assertNotIn("organization_id", get_context())
                    raise RuntimeError("test")
            self.assertEqual(get_context(), original)
            get_context().clear()
            self.assertEqual(get_context(), original)
        self.assertEqual(get_context(), {})

    def test_invalid_or_untrusted_context_keys_do_not_propagate(self):
        with activity_context({"activity_id": "Bearer private", "organization_id": "invalid", "authorization": "private"}):
            self.assertEqual(get_context(), {})

    def test_trusted_tenant_binding_stays_inside_existing_activity_scope(self):
        organization_id = uuid.uuid4()
        activity_id = uuid.uuid4()
        with patch("apps.core.api_activity.services.annotate_activity", return_value=None) as annotate:
            bind_organization(organization_id)
            annotate.assert_not_called()
            self.assertEqual(get_context(), {})
            with activity_context({"activity_id": activity_id}):
                bind_organization(organization_id)
                annotate.assert_called_once_with(activity_id, organization_id=organization_id)
                self.assertEqual(get_context()["organization_id"], str(organization_id))
            self.assertEqual(get_context(), {})

    def test_invalid_tenant_binding_does_not_change_authenticated_context(self):
        original = {"activity_id": uuid.uuid4(), "organization_id": uuid.uuid4()}
        with activity_context(original), patch("apps.core.api_activity.services.annotate_activity") as annotate:
            before = get_context()
            bind_organization("untrusted-invalid-id")
            self.assertEqual(get_context(), before)
            annotate.assert_not_called()

    @override_settings(API_ACTIVITY_ENABLED=False)
    def test_disabled_capture_performs_no_database_access(self):
        with patch("apps.core.api_activity.services.apps.get_model") as get_model:
            self.assertIsNone(start_activity(operation="api.test"))
            self.assertIsNone(record_activity(operation="api.test"))
            self.assertIsNone(finish_activity(uuid.uuid4(), status="failed"))
            get_model.assert_not_called()

    def test_failure_signal_does_not_log_database_exception_content(self):
        with patch("apps.core.api_activity.services.apps.get_model", side_effect=RuntimeError("password=private")):
            with self.assertLogs("shvya.operations", level="WARNING") as logs:
                self.assertIsNone(start_activity(operation="api.test"))
        self.assertNotIn("private", "".join(logs.output))
        self.assertIn("api_activity.capture_failed", "".join(logs.output))

    def test_monitoring_cache_failure_is_visible(self):
        with patch("apps.core.api_activity.services.cache.get_many", side_effect=RuntimeError("down")):
            self.assertFalse(capture_health()["available"])

    def test_invalid_retention_bounds_are_rejected_before_database_access(self):
        for arguments in ({"days": 0}, {"batch_size": 0}, {"max_batches": 1001}):
            with self.assertRaises(ValueError):
                prune_activities(**arguments)

    def test_scheduled_retention_task_is_bounded(self):
        from apps.core.tasks import prune_api_activities

        with patch("apps.core.tasks.prune_activities", return_value={"deleted": 3}) as prune:
            self.assertEqual(prune_api_activities.run(), {"deleted": 3})
        prune.assert_called_once_with(batch_size=500, max_batches=20, max_seconds=30)


@override_settings(API_ACTIVITY_ENABLED=True)
class APIActivityPersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Activity fixture")
        cls.other_organization = Organization.objects.create(name="Other fixture")

    def test_child_inherits_correlation_and_explicit_organization_overrides_context(self):
        parent = start_activity(organization=self.organization, operation="parent")
        with activity_context(parent):
            child = start_activity(operation="child")
            explicit = start_activity(operation="other", organization_id=self.other_organization.pk)
            unscoped = start_activity(operation="platform", organization_id=None)
        self.assertEqual(child.parent_id, parent.id)
        self.assertEqual(child.correlation_id, parent.correlation_id)
        self.assertEqual(child.organization_id, self.organization.id)
        self.assertEqual(explicit.organization_id, self.other_organization.id)
        self.assertIsNone(unscoped.organization_id)
        self.assertEqual(child.organization_reference, str(self.organization.id))

    def test_transition_history_preserves_prior_evidence_and_clears_deadline(self):
        activity = start_activity(
            organization=self.organization, provider_reference="123456789012345678",
            expected_by=timezone.now() + timedelta(minutes=5), next_step="Provider response",
            metadata={"attempt": 1, "token": "never save this"},
        )
        finish_activity(activity, status="accepted", http_status=200, metadata={"provider_state": "accepted"})
        self.assertIsNone(activity.finished_at)
        finish_activity(activity, status="completed", duration_ms=56)
        activity.refresh_from_db()
        self.assertEqual(activity.http_status, 200)
        self.assertEqual(activity.provider_reference, "123456789012345678")
        self.assertEqual(activity.metadata["attempt"], 1)
        self.assertEqual(activity.metadata["token"], REDACTED)
        self.assertIsNotNone(activity.finished_at)
        self.assertIsNone(activity.expected_by)
        self.assertEqual(activity.next_step, "")
        self.assertEqual(list(activity.events.values_list("status", flat=True)), ["processing", "accepted", "completed"])

    def test_append_event_does_not_change_current_state(self):
        activity = start_activity(operation="send")
        record_event(activity, name="callback.received", status="accepted", metadata={"provider_state": "sent"})
        activity.refresh_from_db()
        self.assertEqual(activity.status, "processing")
        self.assertEqual(activity.events.count(), 2)

    def test_annotate_resolves_tenant_after_authentication_and_task_start(self):
        activity = start_activity(kind="task", status="queued")
        annotate_activity(activity, organization_id=self.organization.id, entity_type="lead", entity_id="lead-1", status="processing")
        activity.refresh_from_db()
        self.assertEqual(activity.organization_id, self.organization.id)
        self.assertEqual(activity.entity_id, "lead-1")
        self.assertEqual(activity.status, "processing")

    def test_capture_database_failure_rolls_back_own_savepoint_only(self):
        before = APIActivity.objects.count()
        with transaction.atomic():
            Organization.objects.create(name="Business operation")
            with patch.object(APIActivityEvent.objects, "create", side_effect=IntegrityError("secret SQL body")):
                with self.assertLogs("shvya.operations", level="WARNING") as logs:
                    self.assertIsNone(start_activity(operation="test"))
            self.assertTrue(Organization.objects.filter(name="Business operation").exists())
            self.assertEqual(APIActivity.objects.count(), before)
        self.assertNotIn("secret SQL", "".join(logs.output))

    def test_unknown_outcome_is_not_success_and_invalid_status_is_rejected(self):
        activity = record_activity(status="unknown", error_category="timeout")
        self.assertEqual(activity.status, "unknown")
        with self.assertLogs("shvya.operations", level="WARNING"):
            self.assertIsNone(record_activity(status="succeeded"))

    def test_retention_is_bounded_and_dry_run_does_not_delete(self):
        activities = [record_activity(operation="old") for _ in range(3)]
        ids = [activity.id for activity in activities]
        APIActivity.objects.filter(pk__in=ids).update(created_at=timezone.now() - timedelta(days=45))
        fresh = record_activity(operation="fresh")
        output = StringIO()
        call_command("prune_api_activities", days=30, dry_run=True, stdout=output)
        self.assertIn("Dry run: 3", output.getvalue())
        self.assertTrue(APIActivity.objects.filter(pk=ids[0]).exists())
        call_command("prune_api_activities", days=30, batch_size=1, max_batches=2, stdout=StringIO())
        self.assertEqual(APIActivity.objects.filter(pk__in=ids).count(), 1)
        self.assertTrue(APIActivity.objects.filter(pk=fresh.pk).exists())

    def tearDown(self):
        cache.delete_many(("shvya:api_activity:failed_writes:v1", "shvya:api_activity:last_failure:v1"))
