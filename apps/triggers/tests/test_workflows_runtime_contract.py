"""Workflow contracts on real PostgreSQL; only provider/network edges are mocked."""
import copy
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.channels.tasks import send_whatsapp_message_task
from apps.crm.models import AttributeDefinition, Lead, LeadReminder
from apps.followups.models import LeadSequenceState
from apps.triggers.constants import ACTIONS, TRIGGERS
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun
from apps.triggers.tasks import _dispatch, _dispatch_messages
from services.channels.whatsapp_service import WhatsAppSendError
from services.triggers.actions import deliver_email, execute, workflow_message_block_reason
from services.triggers.evaluator import emit, evaluate, matches, scan_timers
from services.triggers.rules import catalog, save_rule, validate


@pytest.fixture
def env(db):
    # Reuse the established real-model fixture, without inheriting/recounting
    # the existing test methods.
    from .test_smart_triggers import SmartTriggerTests
    fixture = SmartTriggerTests()
    fixture.setUp()
    return fixture


def configuration(env, trigger="lead_created", action="ai"):
    data = copy.deepcopy(env.data)
    data.update(name=f"{trigger} / {action}", trigger_type=trigger, action_type=action)
    c = data["conditions"]
    c.update(duration=1, unit="minutes", keywords=["price"], call_status="completed",
             sequences=[str(env.sequence.id)])
    data["action"] = {
        "start_sequence": {"sequence": str(env.sequence.id), "replace": False},
        "move_stage": {"pipeline": str(env.pipeline.id), "stage": str(env.other_stage.id)},
        "message": {"account": str(env.account.id), "body": "Hi {{lead_name}}", "schedule": "relative", "duration": 0, "unit": "minutes"},
        "email": {"subject": "Hello {{lead_name}}", "body": "Welcome"},
        "reminder": {"duration": 2, "unit": "hours", "note": "Call {{lead_name}}", "overwrite": False},
        "attribute": {"key": "temperature", "value": "Warm"},
        "stop_sequence": {}, "ai": {"enabled": False}, "followup": {"enabled": False},
    }[action]
    return data


def queue_run(env, trigger="lead_created", action="ai", data=None):
    if action == "message":
        WhatsAppMessage.objects.create(organization=env.org, account=env.account,
                                      lead=env.lead, direction="inbound", status="received", body="Hello")
    if action in ("followup", "stop_sequence"):
        LeadSequenceState.objects.create(organization=env.org, lead=env.lead, sequence=env.sequence)
    rule = save_rule(env.user, data or configuration(env, trigger, action))
    payload = {"body": "What is the PRICE?", "status": "completed", "manual": True,
               "sequence": str(env.sequence.id), "entry": env.lead.stage_entered_at.isoformat()}
    if trigger == "no_response":
        msg = WhatsAppMessage.objects.create(organization=env.org, account=env.account,
                                            lead=env.lead, direction="outbound", status="sent", body="Hello")
        payload["sent_event"] = str(TriggerEvent.objects.get(key=f"outbound-sent:{msg.id}").id)
    event = emit(env.lead, trigger, f"matrix:{uuid.uuid4()}", payload)
    evaluate(event.id)
    return TriggerRun.objects.get(rule=rule, event=event)


@pytest.mark.parametrize("trigger", list(TRIGGERS))
@pytest.mark.parametrize("action", list(ACTIONS))
def test_every_trigger_action_pair_executes(env, trigger, action):
    run = queue_run(env, trigger, action)
    execute(run.id)
    run.refresh_from_db()
    if action == "message":
        assert run.status == "scheduled", run.detail
        assert run.finished_at is None
        execute(run.id)
        run.refresh_from_db()
        assert run.status == "queued", run.detail
        assert run.message.body == "Hi Test Lead"
        assert run.message.raw_payload["shvya_workflow"]["run_id"] == str(run.id)
        assert run.finished_at is None
    elif action == "email":
        assert run.status == "email_ready", run.detail
        assert run.finished_at is None
        with patch("services.triggers.actions.send_organization_email", return_value=1) as send:
            deliver_email(run.id)
            deliver_email(run.id)
        send.assert_called_once()
        assert send.call_args.kwargs["to"] == env.lead.email
        run.refresh_from_db()
        assert run.status == "completed", run.detail
    else:
        assert run.status == "completed", run.detail
        env.lead.refresh_from_db()
        if action == "ai":
            assert env.lead.ai_enabled is False
        elif action == "move_stage":
            assert env.lead.stage_id == env.other_stage.id
        elif action == "attribute":
            assert env.lead.attributes["temperature"] == "Warm"
        elif action == "reminder":
            reminder = LeadReminder.objects.get(lead=env.lead)
            assert reminder.description == "Call Test Lead"
            assert reminder.due_at == run.event.created_at + timedelta(hours=2)
        elif action == "start_sequence":
            assert LeadSequenceState.objects.get(lead=env.lead).status == "active"
        elif action == "stop_sequence":
            assert LeadSequenceState.objects.get(lead=env.lead).status == "cleared"
        elif action == "followup":
            assert LeadSequenceState.objects.get(lead=env.lead).lead_auto_followup_enabled is False
    # Re-entering a terminal/queued run must not repeat its CRM mutation.
    before = (LeadReminder.objects.count(), WhatsAppMessage.objects.count())
    execute(run.id)
    assert before == (LeadReminder.objects.count(), WhatsAppMessage.objects.count())


@pytest.mark.parametrize("source", [value for value, _ in Lead._meta.get_field("lead_source").choices])
@pytest.mark.parametrize("trigger", list(TRIGGERS))
def test_creation_source_is_available_for_every_trigger(env, source, trigger):
    data = configuration(env, trigger)
    data["conditions"]["sources"] = [source]
    rule = save_rule(env.user, data)
    env.lead.lead_source = source
    env.lead.attributes["SOURCE"] = "wrong custom answer"
    payload = {"body": "price", "status": "completed", "manual": True, "sequence": str(env.sequence.id)}
    assert matches(rule, env.lead, payload)
    env.lead.lead_source = "external_api" if source != "external_api" else "system"
    env.lead.attributes["SOURCE"] = source
    assert not matches(rule, env.lead, payload)


def test_source_is_canonical_optional_and_not_custom(env):
    before = validate(env.org, env.data)["fingerprint"]
    data = copy.deepcopy(env.data)
    data["conditions"]["sources"] = []
    assert validate(env.org, data)["fingerprint"] == before
    for invalid in (["invented"], "system", [True], ["System"]):
        data["conditions"]["sources"] = invalid
        with pytest.raises(ValidationError):
            validate(env.org, data)
    assert catalog(env.org)["sources"] == dict(Lead._meta.get_field("lead_source").choices)
    assert not AttributeDefinition.objects.filter(organization=env.org, key="lead_source").exists()


def test_real_new_lead_source_event_matches(env):
    data = copy.deepcopy(env.data)
    data["conditions"]["sources"] = ["google_sheets"]
    rule = save_rule(env.user, data)
    lead = Lead.objects.create(organization=env.org, pipeline=env.pipeline, stage=env.stage,
                               name="Sheet lead", phone="+919222222222", lead_source="google_sheets")
    event = TriggerEvent.objects.get(key=f"lead-created:{lead.id}")
    evaluate(event.id)
    execute(TriggerRun.objects.get(rule=rule, event=event).id)
    lead.refresh_from_db()
    assert lead.ai_enabled is False


def test_source_filter_uses_normalized_database_origin(env):
    data = copy.deepcopy(env.data)
    data["conditions"]["sources"] = ["whatsapp"]
    rule = save_rule(env.user, data)
    env.lead.lead_source = "whatsapp_api"
    event = emit(env.lead, "lead_created", f"normalized:{uuid.uuid4()}")
    Lead.objects.filter(pk=env.lead.pk).update(lead_source="whatsapp")
    evaluate(event.id)
    assert TriggerRun.objects.filter(rule=rule, event=event).exists()


def test_can_disable_workflow_with_deleted_selection(env):
    data = configuration(env, action="attribute")
    rule = save_rule(env.user, data)
    env.attribute.delete()
    save_rule(env.user, {"enabled": False}, rule.id)
    rule.refresh_from_db()
    assert not rule.enabled
    with pytest.raises(ValidationError):
        save_rule(env.user, {"enabled": True}, rule.id)
    with pytest.raises(ValidationError):
        save_rule(env.user, {"enabled": "false"}, rule.id)


def test_sequence_status_must_be_persisted(env):
    state = LeadSequenceState.objects.create(organization=env.org, lead=env.lead, sequence=env.sequence)
    state.status = "completed"
    state.lead_auto_followup_enabled = False
    state.save(update_fields=["lead_auto_followup_enabled"])
    assert not TriggerEvent.objects.filter(kind="sequence_ended").exists()
    state.completed_at = timezone.now()
    state.save(update_fields=["status", "completed_at"])
    assert TriggerEvent.objects.filter(kind="sequence_ended").count() == 1


@pytest.mark.parametrize("direction,status", [("inbound", "received"), ("outbound", "sent")])
def test_history_import_does_not_trigger_live_automation(env, direction, status):
    WhatsAppMessage.objects.create(organization=env.org, account=env.account, lead=env.lead,
                                    direction=direction, status=status, body="price", raw_payload={"isHistory": True})
    assert not TriggerEvent.objects.filter(kind__in=["keyword", "outbound_sent"]).exists()


def test_unsaved_outbound_status_does_not_start_timer(env):
    message = WhatsAppMessage.objects.create(organization=env.org, account=env.account,
                                             lead=env.lead, direction="outbound", status="queued")
    message.status = "sent"
    message.body = "unsent"
    message.save(update_fields=["body"])
    assert not TriggerEvent.objects.filter(kind="outbound_sent").exists()


def test_bad_timer_does_not_starve_valid_events(env):
    bad = save_rule(env.user, configuration(env, trigger="stage_idle"))
    bad.conditions = {"duration": 1, "unit": "invalid"}
    bad.save(update_fields=["conditions"])
    run = queue_run(env)
    _dispatch()
    run.refresh_from_db()
    assert run.status == "completed"


def test_inactive_org_timer_is_not_scanned(env):
    save_rule(env.user, configuration(env, trigger="stage_idle"))
    Lead.objects.filter(pk=env.lead.pk).update(stage_entered_at=timezone.now()-timedelta(hours=1))
    env.org.is_active = False
    env.org.save(update_fields=["is_active"])
    scan_timers()
    assert not TriggerEvent.objects.filter(kind="stage_idle").exists()


def queued_message(env):
    run = queue_run(env, action="message")
    execute(run.id)
    execute(run.id)
    run.refresh_from_db()
    return run


def test_message_dispatch_lease_and_inflight_reconciliation(env):
    run = queued_message(env)
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send:
        _dispatch_messages()
        _dispatch_messages()
    send.assert_called_once_with(str(run.message_id))
    run.refresh_from_db()
    assert run.status == "dispatching"
    assert run.finished_at is None
    WhatsAppMessage.objects.filter(pk=run.message_id).update(status="sending", updated_at=timezone.now())
    _dispatch_messages()
    run.refresh_from_db()
    assert run.status == "dispatching"
    WhatsAppMessage.objects.filter(pk=run.message_id).update(status="sent")
    _dispatch_messages()
    run.refresh_from_db()
    assert run.status == "completed" and run.finished_at is not None


@pytest.mark.parametrize("reason", ["disabled", "organization", "deleted", "expired", "sender"])
def test_queued_message_rechecks_before_provider_send(env, reason):
    run = queued_message(env)
    if reason == "disabled":
        SmartTrigger.objects.filter(pk=run.rule_id).update(enabled=False)
    elif reason == "organization":
        env.org.is_active = False
        env.org.save(update_fields=["is_active"])
    elif reason == "deleted":
        run.rule.delete()
    elif reason == "expired":
        WhatsAppMessage.objects.filter(lead=env.lead, direction="inbound").update(created_at=timezone.now()-timedelta(days=2))
    else:
        env.account.is_active = False
        env.account.save(update_fields=["is_active"])
    with patch("services.channels.whatsapp_service.send_outbound_message") as send:
        send_whatsapp_message_task.run(str(run.message_id))
    send.assert_not_called()
    assert WhatsAppMessage.objects.get(pk=run.message_id).status == "failed"


def test_hosted_action_keeps_existing_transport_and_health_gate(env):
    from services.channels.hosted_health_guard import message_is_hosted_automation
    env.account.connection_type = WhatsAppAccount.ConnectionType.coexisted
    env.account.save(update_fields=["connection_type"])
    run = queued_message(env)
    WhatsAppMessage.objects.filter(lead=env.lead, direction="inbound").delete()
    message = WhatsAppMessage.objects.select_related("account").get(pk=run.message_id)
    assert message_is_hosted_automation(message)
    assert workflow_message_block_reason(message) == ""
    assert str(env.account.id) in [str(a["id"]) for a in catalog(env.org)["accounts"]]


@pytest.mark.parametrize("kind", ["network", "exhausted"])
def test_workflow_delivery_never_replays_uncertain_or_exhausted_sends(env, kind):
    run = queued_message(env)
    error = WhatsAppSendError("Provider failed")
    error.__cause__ = WhatsAppAPIError("Provider failed", status_code=None if kind == "network" else 503)
    send_whatsapp_message_task.push_request(retries=3 if kind == "exhausted" else 0)
    try:
        with patch("services.channels.whatsapp_service.send_outbound_message", side_effect=error):
            send_whatsapp_message_task.run(str(run.message_id))
    finally:
        send_whatsapp_message_task.pop_request()
    run.refresh_from_db()
    assert run.status == ("needs_review" if kind == "network" else "failed")
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay") as publish:
        _dispatch_messages()
    publish.assert_not_called()


@pytest.mark.parametrize("reason", ["disabled", "organization", "reply"])
def test_email_rechecks_after_claim(env, reason):
    trigger = "no_response" if reason == "reply" else "lead_created"
    run = queue_run(env, trigger=trigger, action="email")
    execute(run.id)
    if reason == "disabled":
        SmartTrigger.objects.filter(pk=run.rule_id).update(enabled=False)
    elif reason == "organization":
        env.org.is_active = False
        env.org.save(update_fields=["is_active"])
    else:
        WhatsAppMessage.objects.create(organization=env.org, account=env.account, lead=env.lead,
                                        direction="inbound", status="received", body="Reply")
    with patch("services.triggers.actions.send_organization_email") as send:
        deliver_email(run.id)
    send.assert_not_called()
    run.refresh_from_db()
    assert run.status == "skipped"


def test_zero_email_delivery_is_not_reported_success(env):
    run = queue_run(env, action="email")
    execute(run.id)
    with patch("services.triggers.actions.send_organization_email", return_value=0):
        deliver_email(run.id)
    run.refresh_from_db()
    assert run.status == "failed"


def test_relative_schedule_anchors_to_event_time(env):
    run = queue_run(env, action="message")
    event_time = timezone.now()-timedelta(minutes=20)
    TriggerEvent.objects.filter(pk=run.event_id).update(created_at=event_time)
    execute(run.id)
    run.refresh_from_db()
    assert run.due_at == event_time


def test_sender_foreign_org_is_rejected(env):
    from apps.organizations.models import Organization
    foreign = Organization.objects.create(name="Not our tenant")
    account = WhatsAppAccount.objects.create(organization=foreign, business_name="Foreign", phone_number_id="foreign", status="connected")
    data = configuration(env, action="message")
    data["action"]["account"] = str(account.id)
    with pytest.raises(ValidationError):
        validate(env.org, data)
    assert str(account.id) not in [str(a["id"]) for a in catalog(env.org)["accounts"]]
