from types import SimpleNamespace
from unittest.mock import patch

from django.http import Http404
from django.test import TestCase

from apps.ai_engagement.models import AITrace
from apps.ai_engagement.services.trace_sanitizer import MAX_TOTAL_CHARS, sanitize
from apps.ai_engagement.services.trace_service import (
    begin_trace,
    finalize_from_result,
    flush,
    mark_decision,
    mark_error,
    record,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.crm.views.ai_trace import _org_trace
from apps.organizations.models import Organization


class AITracePhase1Tests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Trace Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Lead",
            phone="+919111111111",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="api",
            display_phone_number="+919876543210",
            phone_number_id="p1",
            status="connected",
            is_active=True,
        )
        self.source = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="inbound",
            external_id="wamid-trace-source",
            from_number=self.lead.phone,
            to_number=self.account.display_phone_number,
            body="What is pricing? We receive around 30 leads every day.",
            status="received",
        )

    def _begin(self, *, account=None):
        return begin_trace(
            organization=self.org,
            lead=self.lead,
            account=account or self.account,
            source_message=self.source,
        )

    def _finish(self, token, result):
        finalize_from_result(result)
        flush(reset_token=token)
        return (
            AITrace.objects.filter(organization=self.org)
            .order_by("-started_at")
            .first()
        )

    def test_successful_api_ai_trace(self):
        token = self._begin()
        decision = SimpleNamespace(
            model="gpt-4.1-nano",
            reason_code="ANSWER_ORG_QUESTION",
            reason="",
            should_engage=True,
            silence_rule=None,
            message="Pricing starts from the configured plan.",
            next_requirement_id="daily-leads",
            file_document_id=None,
            backend_revision="r1",
            flow_version="f1",
            qualification_updates=[],
            crm_actions=[],
        )
        mark_decision(decision=decision)
        trace = self._finish(
            token,
            {
                "status": "completed",
                "message_id": str(self.source.id),
                "model": decision.model,
            },
        )
        self.assertEqual(trace.status, AITrace.Status.COMPLETED)
        self.assertEqual(trace.connection_type, "api")
        self.assertIn("Pricing starts", trace.response_preview)

    def test_successful_hosted_ai_trace_uses_canonical_hosted_value(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type", "updated_at"])
        token = self._begin(account=self.account)
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(trace.connection_type, "hosted")

    def test_coexistence_does_not_invent_parallel_connection_value(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type", "updated_at"])
        token = self._begin(account=self.account)
        trace = self._finish(token, {"status": "completed"})
        self.assertNotEqual(trace.connection_type, "coexistence")

    def test_deterministic_qualification_trace(self):
        token = self._begin()
        decision = SimpleNamespace(
            model="deterministic",
            reason_code="QUALIFICATION_NEXT",
            reason="QUALIFICATION_NEXT",
            should_engage=True,
            silence_rule=None,
            message="Are you running ads?",
            next_requirement_id="running-ads",
            file_document_id=None,
            backend_revision="",
            flow_version="flow-1",
            qualification_updates=[
                {
                    "requirement_id": "daily-leads",
                    "value": 30,
                    "source_message_id": str(self.source.id),
                }
            ],
            crm_actions=[],
        )
        mark_decision(decision=decision)
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(trace.execution_path, "DETERMINISTIC")
        accepted = trace.details["qualification"]["updates_accepted"]
        self.assertEqual(accepted[0]["value"], 30)

    def test_model_generated_response_trace(self):
        token = self._begin()
        mark_decision(
            decision=SimpleNamespace(
                model="gpt-4.1-nano",
                reason_code="NORMAL_CONVERSATION",
                reason="",
                should_engage=True,
                silence_rule=None,
                message="Hello from model",
                next_requirement_id=None,
                file_document_id=None,
                backend_revision="",
                flow_version="",
                qualification_updates=[],
                crm_actions=[],
            )
        )
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(trace.execution_path, "MODEL")
        self.assertEqual(trace.response_preview, "Hello from model")

    def test_rag_retrieval_trace_has_references_not_contents(self):
        token = self._begin()
        record(
            "rag",
            {
                "invoked": True,
                "retrieval_path": "SEMANTIC",
                "retrieved": [
                    {"document_id": "10", "chunk_id": "20", "score": 0.91}
                ],
            },
        )
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(trace.details["rag"]["retrieved"][0]["chunk_id"], "20")
        self.assertNotIn("content", trace.details["rag"]["retrieved"][0])

    def test_ai_disabled_or_blocked_trace(self):
        token = self._begin()
        record(
            "permission",
            {
                "final_permission_result": False,
                "reason": "organization_ai_disabled",
            },
        )
        trace = self._finish(
            token,
            {"status": "skipped", "reason": "organization_ai_disabled"},
        )
        self.assertEqual(trace.status, AITrace.Status.BLOCKED)
        self.assertEqual(trace.reason_code, "organization_ai_disabled")

    def test_stale_message_trace(self):
        token = self._begin()
        trace = self._finish(
            token,
            {"status": "skipped", "reason": "conversation_changed_before_send"},
        )
        self.assertEqual(trace.status, AITrace.Status.STALE)
        self.assertEqual(trace.details["finalization"]["freshness_check"], "rejected")

    def test_duplicate_response_trace(self):
        token = self._begin()
        trace = self._finish(
            token,
            {"status": "skipped", "reason": "duplicate_ai_response"},
        )
        self.assertEqual(trace.status, AITrace.Status.DUPLICATE)
        duplicate_check = trace.details["finalization"]["duplicate_response_check"]
        self.assertEqual(duplicate_check, "rejected")

    def test_provider_failure_trace(self):
        token = self._begin()
        mark_error(
            step="generation",
            exc=RuntimeError("provider failed"),
            retryable=True,
            code="PROVIDER_FAILURE",
        )
        trace = self._finish(
            token,
            {"status": "failed", "reason": "engagement_generation_failed"},
        )
        self.assertEqual(trace.status, AITrace.Status.FAILED)
        self.assertEqual(trace.details["error"]["processing_step"], "generation")
        self.assertTrue(trace.details["error"]["retryable"])

    def test_crm_action_trace(self):
        token = self._begin()
        record(
            "crm_actions",
            {
                "proposed": [{"type": "add_note"}],
                "attempts": [
                    {
                        "accepted": [{"type": "add_note", "status": "executed"}],
                        "rejected": [],
                    }
                ],
            },
        )
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(trace.details["crm_actions"]["proposed"][0]["type"], "add_note")

    def test_stage_pipeline_action_trace(self):
        token = self._begin()
        record(
            "crm_actions",
            {
                "proposed": [
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {"stage_id": str(self.stage.id)},
                    }
                ],
                "attempts": [
                    {
                        "accepted": [
                            {
                                "type": "pipeline_transition",
                                "status": "executed",
                                "pipeline_id": str(self.pipeline.id),
                                "stage_id": str(self.stage.id),
                            }
                        ]
                    }
                ],
            },
        )
        trace = self._finish(token, {"status": "completed"})
        accepted = trace.details["crm_actions"]["attempts"][0]["accepted"][0]
        self.assertEqual(accepted["pipeline_id"], str(self.pipeline.id))

    def test_cross_organization_trace_access_prevention(self):
        token = self._begin()
        trace = self._finish(token, {"status": "completed"})
        other = Organization.objects.create(name="Other")
        with self.assertRaises(Http404):
            _org_trace(other, trace.id)

    def test_trace_layer_does_not_copy_knowledge_content(self):
        token = self._begin()
        record(
            "rag",
            {"retrieved": [{"document_id": "owned-doc", "chunk_id": "owned-chunk"}]},
        )
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(
            set(trace.details["rag"]["retrieved"][0]),
            {"document_id", "chunk_id"},
        )

    @patch(
        "apps.ai_engagement.models.AITrace.objects.create",
        side_effect=RuntimeError("trace db down"),
    )
    def test_trace_write_failure_does_not_raise(self, _create):
        token = self._begin()
        record("permission", {"final_permission_result": True})
        finalize_from_result({"status": "completed"})
        flush(reset_token=token)
        self.assertEqual(AITrace.objects.filter(organization=self.org).count(), 0)

    def test_trace_detail_lookup_is_organization_scoped(self):
        token = self._begin()
        trace = self._finish(token, {"status": "completed"})
        self.assertEqual(_org_trace(self.org, trace.id).id, trace.id)

    def test_secret_fields_are_never_persisted_by_sanitizer(self):
        value = sanitize(
            {
                "access_token": "secret",
                "authorization": "Bearer abc",
                "nested": {"password": "pw", "client_secret": "hidden"},
                "safe": "x" * (MAX_TOTAL_CHARS * 2),
            }
        )
        self.assertEqual(value["access_token"], "[redacted]")
        self.assertEqual(value["authorization"], "[redacted]")
        self.assertEqual(value["nested"]["password"], "[redacted]")
        self.assertLessEqual(len(value["safe"]), 1200)

    def test_trace_history_is_append_only_per_attempt(self):
        first_token = self._begin()
        first = self._finish(first_token, {"status": "completed"})
        second_token = self._begin()
        second = self._finish(second_token, {"status": "completed"})
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(AITrace.objects.filter(organization=self.org).count(), 2)
