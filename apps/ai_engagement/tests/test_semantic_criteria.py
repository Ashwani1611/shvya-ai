"""Only current, source-bound semantic evidence may authorize qualification."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core import signing
import pytest

from apps.ai_engagement.services.qualification_check import QualificationCheckError
from apps.ai_engagement.services.semantic_criteria import (
    RECEIPT_KEY,
    RECEIPT_SALT,
    criteria_clauses,
    snapshot_fingerprint,
    validate_evaluations,
    verified_semantic_criteria_for_lead,
)


def snapshot():
    return {
        "organization_id": "org-a", "lead_id": "lead-a", "stage_id": "new", "pipeline_id": "leads",
        "ai_playbook": "## Qualification Criteria\nThe prospect intends to operate a resort.",
        "ai_enabled": True,
        "requirements": [{"id": "use_case", "label": "Intended use", "required": True}],
        "answers": {"use_case": {"status": "answered", "value": "Resort", "source_message_id": "inbound-1"}},
        "sources": {"inbound-1": "I intend to operate a resort."},
        "latest_inbound": {"id": "inbound-1", "body": "I intend to operate a resort."},
        "acknowledgment_message_id": None,
    }


def evaluation():
    return {"evaluations": [{"criterion_id": "criterion_1", "verdict": "pass", "evidence": [
        {"requirement_id": "use_case", "source_message_id": "inbound-1", "quote": "I intend to operate a resort."},
    ]}]}


def test_semantic_pass_requires_complete_exact_current_inbound_evidence():
    context = snapshot()
    results = validate_evaluations(evaluation(), clauses=criteria_clauses(context["ai_playbook"]), snapshot=context)
    assert results[0]["verdict"] == "pass"


@pytest.mark.parametrize("mutation", [
    lambda result: result.update(qualified=True),
    lambda result: result["evaluations"].clear(),
    lambda result: result["evaluations"][0].update(criterion_id="invented"),
    lambda result: result["evaluations"][0].update(evidence=[]),
    lambda result: result["evaluations"][0]["evidence"][0].update(requirement_id="other"),
    lambda result: result["evaluations"][0]["evidence"][0].update(source_message_id="other-tenant-message"),
    lambda result: result["evaluations"][0]["evidence"][0].update(quote="I am ready to purchase now."),
])
def test_model_verdict_cannot_bypass_evidence_or_complete_coverage(mutation):
    context, result = snapshot(), evaluation()
    mutation(result)
    with pytest.raises(QualificationCheckError):
        validate_evaluations(result, clauses=criteria_clauses(context["ai_playbook"]), snapshot=context)


def test_and_conditions_cannot_be_omitted():
    clauses = criteria_clauses("## Qualification Criteria\nThe prospect intends to operate a resort and owns usable land.")
    assert len(clauses) == 2
    with pytest.raises(QualificationCheckError):
        validate_evaluations(evaluation(), clauses=clauses, snapshot=snapshot())


def test_deterministic_failed_threshold_cannot_be_overruled_by_model():
    context = snapshot()
    context["requirements"] = [{"id": "budget", "label": "Budget", "required": True}]
    context["answers"] = {"budget": {"status": "answered", "value": 5000, "source_message_id": "inbound-1"}}
    context["sources"] = {"inbound-1": "5000"}
    result = {"evaluations": [{"criterion_id": "criterion_1", "verdict": "pass", "evidence": [
        {"requirement_id": "budget", "source_message_id": "inbound-1", "quote": "5000"}]}]}
    rules = validate_evaluations(result, clauses=criteria_clauses("## Qualification Criteria\nBudget >= 50000"), snapshot=context)
    assert rules[0]["verdict"] != "pass"


@pytest.mark.parametrize("criteria", [
    "Acknowledgment message has been sent.",
    "The lead has paid the deposit.",
    "The lead is ready or has secured funding.",
])
def test_claimed_actions_or_ambiguous_conditions_fail_closed(criteria):
    rules = validate_evaluations(evaluation(), clauses=criteria_clauses("## Qualification Criteria\n" + criteria), snapshot=snapshot())
    assert rules[0]["verdict"] == "unknown"


def test_signed_receipt_must_match_current_tenant_playbook_answers_and_latest_inbound():
    original = snapshot()
    receipt = signing.dumps({"fingerprint": snapshot_fingerprint(original), "evaluations": evaluation()}, salt=RECEIPT_SALT)
    lead = SimpleNamespace(attributes={RECEIPT_KEY: receipt})
    args = (lead, original["ai_playbook"], original["requirements"], {})
    with patch("apps.ai_engagement.services.semantic_criteria._snapshot", return_value=original):
        assert verified_semantic_criteria_for_lead(*args)["qualified"] is True
    for key, value in [
        ("organization_id", "org-b"), ("lead_id", "lead-b"), ("stage_id", "manual-stage"),
        ("ai_playbook", "Changed policy"), ("latest_inbound", {"id": "new-message", "body": "Actually no."}),
        ("answers", {"use_case": {"status": "unknown"}}),
    ]:
        changed = deepcopy(original)
        changed[key] = value
        with patch("apps.ai_engagement.services.semantic_criteria._snapshot", return_value=changed):
            assert verified_semantic_criteria_for_lead(*args) is None


def test_unsigned_client_attribute_cannot_authorize_qualification():
    lead = SimpleNamespace(attributes={RECEIPT_KEY: '{"qualified":true}'})
    with patch("apps.ai_engagement.services.semantic_criteria._snapshot") as context:
        assert verified_semantic_criteria_for_lead(lead, "", [], {}) is None
        context.assert_not_called()


def test_expired_receipt_fails_closed():
    lead = SimpleNamespace(attributes={RECEIPT_KEY: "old-signed-receipt"})
    with patch("apps.ai_engagement.services.semantic_criteria.signing.loads", side_effect=signing.SignatureExpired):
        assert verified_semantic_criteria_for_lead(lead, "", [], {}) is None

@pytest.mark.django_db(transaction=True)
def test_background_verification_persists_receipt_without_provider_inside_transaction():
    from django.db import connection
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.semantic_criteria import refresh_semantic_criteria
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage
    from apps.crm.models import Lead, Pipeline, Stage
    from apps.organizations.models import Organization
    organization = Organization.objects.create(name="Evidence test")
    raw = snapshot()["ai_playbook"]
    OrgInfo.objects.create(organization=organization, ai_playbook=raw, ai_enabled=True)
    pipeline = Pipeline.objects.create(organization=organization, name="Semantic Sales")
    stage = Stage.objects.create(pipeline=pipeline, name="New Lead", display_order=0)
    lead = Lead.objects.create(organization=organization, pipeline=pipeline, stage=stage,
                               name="Example", phone="+919876500101", ai_enabled=True)
    account = WhatsAppAccount.objects.create(organization=organization, business_name="Example", is_active=True)
    inbound = WhatsAppMessage.objects.create(organization=organization, account=account, lead=lead,
        direction="inbound", body="I intend to operate a resort.", status="received", external_id="semantic-inbound")
    requirements = snapshot()["requirements"]
    state = {"qualification_status": "completed", "requirement_states": {
        "use_case": {"status": "answered", "value": "Resort", "source_message_id": str(inbound.id)},
    }}
    output = evaluation()
    output["evaluations"][0]["evidence"][0]["source_message_id"] = str(inbound.id)
    calls = []

    def evaluate(**kwargs):
        assert not connection.in_atomic_block
        calls.append(kwargs)
        return output

    service = SimpleNamespace(evaluate_criteria=evaluate)
    with patch("apps.ai_engagement.services.transactional_turn_runtime._requirements_for_turn", return_value=requirements), patch(
        "apps.ai_engagement.services.qualification_state.state_for_lead", return_value=state,
    ):
        result = refresh_semantic_criteria(service=service, organization=organization, lead=lead)
        assert result["status"] == "verified"
        lead.refresh_from_db()
        assert verified_semantic_criteria_for_lead(lead, raw, requirements, state)["qualified"] is True
        assert refresh_semantic_criteria(service=service, organization=organization, lead=lead)["status"] == "unchanged"
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_stage_change_discards_generated_receipt():
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.semantic_criteria import refresh_semantic_criteria
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage
    from apps.crm.models import Lead, Pipeline, Stage
    from apps.organizations.models import Organization
    organization = Organization.objects.create(name="Concurrent evidence test")
    raw = snapshot()["ai_playbook"]
    OrgInfo.objects.create(organization=organization, ai_playbook=raw, ai_enabled=True)
    pipeline = Pipeline.objects.create(organization=organization, name="Semantic Sales")
    stage = Stage.objects.create(pipeline=pipeline, name="New Lead", display_order=0)
    manual_stage = Stage.objects.create(pipeline=pipeline, name="Human Review", display_order=99)
    lead = Lead.objects.create(organization=organization, pipeline=pipeline, stage=stage,
                               name="Example", phone="+919876500102", ai_enabled=True)
    account = WhatsAppAccount.objects.create(organization=organization, business_name="Example", is_active=True)
    inbound = WhatsAppMessage.objects.create(organization=organization, account=account, lead=lead,
        direction="inbound", body="I intend to operate a resort.", status="received", external_id="semantic-race")
    requirements = snapshot()["requirements"]
    state = {"qualification_status": "completed", "requirement_states": {
        "use_case": {"status": "answered", "value": "Resort", "source_message_id": str(inbound.id)},
    }}
    output = evaluation()
    output["evaluations"][0]["evidence"][0]["source_message_id"] = str(inbound.id)

    def evaluate(**kwargs):
        Lead.objects.filter(id=lead.id, organization=organization).update(stage=manual_stage)
        return output

    with patch("apps.ai_engagement.services.transactional_turn_runtime._requirements_for_turn", return_value=requirements), patch(
        "apps.ai_engagement.services.qualification_state.state_for_lead", return_value=state,
    ):
        result = refresh_semantic_criteria(service=SimpleNamespace(evaluate_criteria=evaluate), organization=organization, lead=lead)
    assert result["status"] == "stale"
    lead.refresh_from_db()
    assert RECEIPT_KEY not in lead.attributes
