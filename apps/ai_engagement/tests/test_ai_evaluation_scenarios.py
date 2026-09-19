"""Exercise persisted CRM + installed graph/finalizer with recorded provider responses.

No live provider is called and no delivery task is dispatched. This tests backend
behaviour, not the open-ended quality of the configured production language model.
"""
from tests.playbook_fixtures import build_ai_playbook

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.ai_engagement.evaluation.scenarios import load_scenarios
from apps.ai_engagement.models import AITrace, Chunk, Document, OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.embeddings import EmbeddingError
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import record_last_asked_requirement, state_for_lead
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Stage
from apps.organizations.models import Organization

SCENARIOS = load_scenarios(os.environ.get("SHVYA_AI_EVALUATION_SCENARIOS"))


class NoRetry:
    def retry(self, **kwargs):
        raise AssertionError(f"Unexpected runtime retry: {kwargs}")


def assert_expected(condition, category, message):
    assert condition, f"[{category}] {message}"


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["api", "hosted"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["id"])
def test_conversation_scenario(scenario, transport, record_property):
    record_property("evaluation_category", scenario.get("category", "behaviour"))
    record_property("transport", transport)
    cache.clear()
    world = SimpleNamespace()
    fixtures.AIEngagementControlTests.setUp(world)
    org, lead, account = world.organization, world.lead, world.account
    org.settings = scenario.get("organization", {})
    org.save(update_fields=["settings"])
    if transport == "hosted":
        account.connection_type = WhatsAppAccount.ConnectionType.coexisted
        account.save(update_fields=["connection_type"])
    target = Stage.objects.create(pipeline=world.pipeline, name="Ready for review", display_order=100, ai_on=True)
    questions = scenario.get("questions", [])
    for item in questions:
        AttributeDefinition.objects.create(organization=org, name=item["field"], key=item["field"], field_type=item.get("field_type", "text"))
    requirements_text = "\n".join(f"[id: {item['id']}] {item['question']}" for item in questions)
    if questions:
        requirements_text += "\nAll questions are required"
    instructions = ("Be concise and answer customer questions using verified organization information.\n"
                    "## Attribute mapped\n" + "\n".join(f"{q['id']} -> {q['field']}" for q in questions) +
                    "\n## Stage shifting\nWhen all required qualification questions are answered, move to Ready for review.\n"
                    'Acknowledgment message: "Your details are complete. Thank you."')
    info, _ = OrgInfo.objects.get_or_create(organization=org)
    info.ai_playbook = build_ai_playbook(questions=requirements_text, rules=instructions)

    info.bot_languages = "English, Hindi, Hinglish"
    info.about = str(scenario.get("about", ""))
    info.ai_enabled = True
    info.save()
    requirements = compile_qualification_requirements(requirements_text)["requirements"]
    requirement_ids = {item.get("stable_id", item["id"]): item["id"] for item in requirements}
    if scenario.get("initial_question"):
        record_last_asked_requirement(lead, requirement_ids[scenario["initial_question"]], requirements=requirements)
        # An actual earlier bot question establishes history and greeting state.
        WhatsAppMessage.objects.create(organization=org, account=account, lead=lead,
            direction="outbound", body=next(q["question"] for q in questions if q["id"] == scenario["initial_question"]),
            status="sent", from_number=account.display_phone_number, to_number=lead.phone)
    if scenario.get("foreign_knowledge"):
        foreign = Organization.objects.create(name="Private other organization")
        doc = Document.objects.create(organization=foreign, name="Foreign price", source_key="foreign.txt", processing_status="completed")
        Chunk.objects.create(organization=foreign, document=doc, content=scenario["foreign_knowledge"])

    for index, turn in enumerate(scenario["turns"]):
        source = WhatsAppMessage.objects.create(organization=org, account=account, lead=lead, direction="inbound",
            external_id=f"eval-{scenario['id']}-{transport}-{index}", body=turn["message"], status="received",
            from_number=lead.phone, to_number=account.display_phone_number)
        expected = turn["expect"]
        calls = []

        def recorded_provider(**kwargs):
            metadata = kwargs.get("metadata") or {}
            calls.append(metadata.get("phase", "unknown"))
            phase = metadata.get("phase", "")
            if phase == "intent_classification":
                payload = {"primary_intent": turn.get("intent_fixture", "UNKNOWN"), "secondary_intents": [],
                           "confidence": 0.95, "entities": [], "facts": [], "qualification_candidate": None,
                           "direct_question": turn["message"] if "?" in turn["message"] else None,
                           "requested_action": None, "language": turn.get("language", "en"),
                           "requires_knowledge": True, "requires_human": False}
            elif phase == "grounding":
                payload = {"approved": True, "reason": "Recorded verifier response"}
            else:
                payload = {"should_engage": True, "silence_rule": None,
                           "message": turn.get("model_reply", "Thank you.") + ("\n\n" + next(q["question"] for q in questions if q["id"] == expected["next"]) if expected.get("next") else ""), "file_document_id": None,
                           "crm_actions": [], "qualification_updates": [],
                           "next_requirement_id": requirement_ids.get(expected.get("next")), "reason_code": "NORMAL_CONVERSATION"}
            return AITextResult(json.dumps(payload, ensure_ascii=False), "recorded-fixture-model")

        with (patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.__init__", return_value=None),
              patch("apps.ai_engagement.services.ai_provider.OpenAIProvider.generate_text", side_effect=recorded_provider),
              patch("apps.ai_engagement.services.embeddings.EmbeddingService.embed_text", side_effect=EmbeddingError("Recorded keyword fallback"))):
            if transport == "api":
                from apps.ai_engagement import tasks
                def run():
                    return tasks._execute_ai_engagement_response_impl(task=NoRetry(), lead_id=str(lead.pk))
            else:
                from apps.hosted_automation.models import HostedAutomationJob
                from apps.hosted_automation import execution
                job = HostedAutomationJob.objects.create(organization=org, account=account, lead=lead,
                                                        source_message=source, available_at=timezone.now())
                def run():
                    return execution.execute_hosted_ai_engagement(task=NoRetry(), job=job)
            result = run()
            assert_expected(result.get("status") == "completed" or (expected.get("disabled") and result.get("reason") == "lead_ai_disabled"), "actions", f"Runtime did not complete: {result}")
            outbound = WhatsAppMessage.objects.filter(organization=org, lead=lead, account=account, direction="outbound",
                raw_payload__shvya_ai__source_inbound_message_id=str(source.pk))
            assert_expected(outbound.count() == (1 if expected.get("reply", True) else 0), "actions", "Unexpected reply/silence or duplicate reply")
            body = outbound.first().body if outbound.exists() else ""
            for text in expected.get("forbidden", []):
                assert_expected(text.casefold() not in body.casefold(), "hallucination", f"Unsupported content: {text}")
            for text in expected.get("contains", []):
                assert_expected(text in body, "language", f"Expected grounded content: {text}")
            lead.refresh_from_db()
            for key, value in expected.get("attributes", {}).items():
                assert_expected(lead.attributes.get(key) == value, "qualification", f"{key}: {lead.attributes.get(key)!r} != {value!r}")
            if expected.get("disabled"):
                assert_expected(not lead.ai_enabled, "silence", "Explicit opt-out did not disable lead AI")
            state = state_for_lead(lead, requirements=requirements)
            if expected.get("next"):
                question = next(item for item in requirements if item["id"] == requirement_ids[expected["next"]])
                question_line = question["question"].splitlines()[0]
                assert_expected(question_line in body, "qualification", f"Next question missing: {question_line}; reply={body}")
                if expected.get("answer_before_question"):
                    answer = expected["answer_before_question"]
                    assert_expected(answer in body and body.index(answer) < body.index(question_line), "qualification", "Answer must precede next question")
                for option in question.get("options", []):
                    assert_expected(option["value"] in body, "qualification", "Configured option missing")
            if expected.get("completed"):
                assert_expected(state["qualification_status"] == "completed" and lead.stage_id == target.pk,
                                "qualification", "Configured completion state/stage not committed")
            if expected.get("no_question"):
                assert_expected(not any(q["question"].splitlines()[0] in body for q in questions), "qualification", "Repeated answered question")
            trace = AITrace.objects.filter(organization=org, source_inbound_message_id=source.pk).latest("started_at")
            details = trace.details
            if expected.get("grounding"):
                assert_expected(details.get("grounding", {}).get("category") == expected["grounding"], "rag", f"Unexpected evidence: {details.get('grounding')}")
            if expected.get("intent"):
                observed = details.get("intent", {})
                assert_expected(expected["intent"] in str(observed), "intent", f"Intent missing: {observed}")
            before = outbound.count()
            run()
            assert_expected(outbound.count() == before, "actions", "Replay produced a duplicate reply")
            assert_expected(calls.count("intent_classification") <= 1, "actions", f"Repeated intent provider calls: {calls}")
