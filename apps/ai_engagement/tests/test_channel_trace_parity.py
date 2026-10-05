"""Durable channel observations must retain tenant and preview boundaries."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import AITrace
from apps.ai_engagement.services import trace_service as traces
from apps.ai_engagement.services.instagram_trace import traced_instagram_turn
from apps.ai_engagement.services.playground import PlaygroundError, PlaygroundResult
from apps.ai_engagement.services.tenant_guard import TenantGuard, TenantScopeError
from apps.ai_engagement.services.turn_diagnostics import sandbox_diagnostics
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage
from apps.crm.models import Lead
from apps.crm.views.ai_trace import _org_trace
from apps.organizations.models import Organization


def preview_result():
    return PlaygroundResult(session_id="preview", message="Hello", response="Preview answer",
                            should_engage=True, knowledge=[], model="test-model",
                            brain_bundle={"revision": "revision-1"})


class SandboxTraceContractTests(SimpleTestCase):
    def setUp(self):
        self.org = SimpleNamespace(id=uuid4())

    def test_preview_trace_is_distinct_sanitized_and_has_no_real_lead(self):
        with patch("apps.ai_engagement.models.AITrace.objects.create") as create, \
             patch("apps.ai_engagement.models.AITrace.objects.filter") as query:
            # Creation's atomic scope is observed separately from its database
            # writes here; database constraint coverage is in the TestCase below.
            with patch("django.db.transaction.atomic", side_effect=nullcontext):
                for unused in range(2):
                    token = traces.begin_sandbox_trace(organization=self.org, message="password=hidden-token")
                    traces.current().data["status"] = "completed"
                    traces.flush(reset_token=token)
        first, second = (call.kwargs for call in create.call_args_list)
        self.assertIsNone(first["lead"])
        self.assertEqual(first["connection_type"], "sandbox")
        self.assertNotEqual(first["source_inbound_message_id"], second["source_inbound_message_id"])
        self.assertNotIn("hidden-token", json.dumps(first, default=str))
        self.assertEqual(query.call_count, 2)

    def test_preview_storage_failure_does_not_change_result_or_parent_scope(self):
        parent = traces.begin_trace(organization=self.org)
        previous = traces.current()
        @sandbox_diagnostics
        def run(self, **kwargs):
            return preview_result()
        try:
            with patch("apps.ai_engagement.models.AITrace.objects.create", side_effect=RuntimeError("storage unavailable")), \
                 patch("django.db.transaction.atomic", side_effect=nullcontext):
                result = run(None, organization=self.org, message="Hello")
            self.assertEqual(result.response, "Preview answer")
            self.assertIsNone(result.trace_id)
            self.assertIs(traces.current(), previous)
        finally:
            traces.flush(reset_token=parent)

    def test_sandbox_trace_guard_cannot_be_used_as_a_real_lead_trace(self):
        trace = SimpleNamespace(organization_id=self.org.id, connection_type="sandbox", lead_id=None)
        self.assertIs(TenantGuard(self.org).validate_trace(trace), trace)
        with self.assertRaises(TenantScopeError):
            TenantGuard(self.org).validate_trace(trace, lead=SimpleNamespace(id="some-lead"))
        trace.organization_id = uuid4()
        with self.assertRaises(TenantScopeError):
            TenantGuard(self.org).validate_trace(trace)


class ChannelTracePersistenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Trace channels")
        self.other = Organization.objects.create(name="Unrelated trace workspace")
        pipeline = self.org.pipelines.first()
        self.lead = Lead.objects.create(organization=self.org, pipeline=pipeline,
                                       stage=pipeline.stages.first(), name="Instagram test", phone="")
        self.account = InstagramAccount.objects.create(organization=self.org, ig_user_id="trace-business")
        self.conversation = InstagramConversation.objects.create(
            organization=self.org, account=self.account, lead=self.lead, participant_id="trace-customer",
        )
        self.source = InstagramMessage.objects.create(
            organization=self.org, account=self.account, conversation=self.conversation,
            direction="inbound", status="received", body="What is pricing? password=hidden-token",
        )

    def test_instagram_exact_source_is_durable_and_tenant_scoped(self):
        @traced_instagram_turn
        def run(**kwargs):
            traces.mark_decision(decision=SimpleNamespace(model="test-model", should_engage=True,
                                                        message="A sanitized answer", reason_code="ANSWER_ORG_QUESTION"))
            return {"status": "completed", "source_message_id": str(self.source.pk)}
        result = run(task=None, message_id=self.source.pk)
        self.assertEqual(result["status"], "completed")
        trace = AITrace.objects.get(organization=self.org)
        self.assertEqual(trace.connection_type, "instagram")
        self.assertEqual(trace.source_inbound_message_id, self.source.pk)
        self.assertEqual(trace.lead_id, self.lead.pk)
        self.assertIsNone(trace.whatsapp_account_id)
        self.assertEqual(trace.details["identity"]["instagram_account_id"], str(self.account.pk))
        self.assertEqual(trace.details["finalization"]["whatsapp_24h_eligibility"], "not_applicable")
        self.assertNotIn("hidden-token", json.dumps(trace.details))
        self.assertEqual(trace.response_preview, "A sanitized answer")
        with self.assertRaises(Http404):
            _org_trace(self.other, trace.pk)

    def test_sandbox_operations_metadata_has_no_fake_lead_and_names_channel(self):
        from apps.integrations.operations.tools.traces import get_production_trace
        token = traces.begin_sandbox_trace(organization=self.org, message="Preview")
        traces.flush(reset_token=token)
        trace = AITrace.objects.get(organization=self.org)
        with patch("apps.integrations.operations.tools.traces._organization_for", return_value=self.org), \
             patch("apps.integrations.operations.tools.traces._require_operations_capability") as capability:
            result = get_production_trace(identity=object(), arguments={"trace_id": str(trace.pk)})
        self.assertIsNone(result.data["trace"]["lead_id"])
        self.assertEqual(result.data["trace"]["connection_type"], "sandbox")
        self.assertEqual(result.data["content_access"], "metadata_only")
        self.assertNotIn("content", result.data)
        capability.assert_called_once()

    def test_instagram_lead_created_by_executor_attaches_same_observed_turn(self):
        self.conversation.lead = None
        self.conversation.save(update_fields=["lead"])
        @traced_instagram_turn
        def run(**kwargs):
            traces.record("generation", model="model-after-create", generated_response="Recorded before trace row")
            InstagramConversation.objects.filter(pk=self.conversation.pk).update(lead=self.lead)
            with self.captureOnCommitCallbacks(execute=True):
                InstagramMessage.objects.create(
                    organization=self.org, account=self.account, conversation=self.conversation,
                    direction="outbound", status="read", body="Final reply",
                    raw_payload={"shvya_ai": {"source_inbound_message_id": str(self.source.pk)}},
                )
            return {"status": "completed"}
        run(task=None, message_id=self.source.pk)
        trace = AITrace.objects.get(organization=self.org)
        self.assertEqual(trace.lead_id, self.lead.pk)
        self.assertEqual(trace.response_preview, "Recorded before trace row")
        self.assertEqual(trace.details["delivery"]["status"], "read")

    def test_instagram_delivery_callback_updates_only_its_trace(self):
        token = traces.begin_trace(organization=self.org, lead=self.lead,
                                   source_message=self.source, account=self.account)
        traces.finalize_from_result({"status": "completed"})
        traces.flush(reset_token=token)
        with self.captureOnCommitCallbacks(execute=True):
            outbound = InstagramMessage.objects.create(
                organization=self.org, account=self.account, conversation=self.conversation,
                direction="outbound", status="read", body="Reply",
                raw_payload={"shvya_ai": {"source_inbound_message_id": str(self.source.pk)}},
            )
        trace = AITrace.objects.get(organization=self.org)
        self.assertEqual(trace.outbound_message_id, outbound.pk)
        self.assertEqual(trace.details["delivery"]["status"], "read")

    def test_instagram_foreign_lead_is_rejected_before_execution(self):
        pipeline = self.other.pipelines.first()
        foreign = Lead.objects.create(organization=self.other, pipeline=pipeline,
                                      stage=pipeline.stages.first(), name="Other lead", phone="")
        self.conversation.lead = foreign
        self.conversation.save(update_fields=["lead"])
        @traced_instagram_turn
        def run(**kwargs):
            self.fail("Cross-organization turn executed")
        with self.assertRaises(TenantScopeError):
            run(task=None, message_id=self.source.pk)
        self.assertFalse(AITrace.objects.exists())

    def test_delivery_observed_before_turn_flush_survives_with_generation(self):
        @traced_instagram_turn
        def run(**kwargs):
            traces.record("generation", model="test-model", generated_response="Final reply")
            with self.captureOnCommitCallbacks(execute=True):
                outbound = InstagramMessage.objects.create(
                    organization=self.org, account=self.account, conversation=self.conversation,
                    direction="outbound", status="read", body="Final reply",
                    raw_payload={"shvya_ai": {"source_inbound_message_id": str(self.source.pk)}},
                )
            self.assertEqual(AITrace.objects.get(organization=self.org).details["delivery"]["status"], "read")
            return {"status": "completed", "message_id": str(outbound.pk)}
        run(task=None, message_id=self.source.pk)
        trace = AITrace.objects.get(organization=self.org)
        self.assertEqual(trace.details["delivery"]["status"], "read")
        self.assertEqual(trace.response_preview, "Final reply")

    def test_sandbox_turn_persists_without_customer_or_message_creation(self):
        before = Lead.objects.count(), InstagramMessage.objects.count()
        @sandbox_diagnostics
        def run(self, **kwargs):
            return preview_result()
        result = run(None, organization=self.org, message="Hello")
        trace = _org_trace(self.org, result.trace_id)
        self.assertIsNone(trace.lead_id)
        self.assertEqual(trace.connection_type, "sandbox")
        self.assertEqual(trace.status, "completed")
        self.assertFalse(trace.details["finalization"]["live_actions_executed"])
        self.assertEqual(before, (Lead.objects.count(), InstagramMessage.objects.count()))
        with self.assertRaises(Http404):
            _org_trace(self.other, trace.pk)

    def test_sandbox_exception_is_durable_and_not_a_completed_action(self):
        @sandbox_diagnostics
        def run(self, **kwargs):
            raise PlaygroundError("Provider failed password=hidden-token")
        with self.assertRaises(PlaygroundError):
            run(None, organization=self.org, message="Hello")
        trace = AITrace.objects.get(organization=self.org)
        self.assertEqual(trace.status, "failed")
        self.assertNotIn("hidden-token", json.dumps(trace.details))
        self.assertIsNone(trace.outbound_message_id)

    def test_database_constraint_requires_real_lead_for_live_trace(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AITrace.objects.create(organization=self.org, lead=None, connection_type="instagram",
                                   source_inbound_message_id=uuid4())
        with self.assertRaises(IntegrityError), transaction.atomic():
            AITrace.objects.create(organization=self.org, lead=self.lead, connection_type="sandbox",
                                   source_inbound_message_id=uuid4())
