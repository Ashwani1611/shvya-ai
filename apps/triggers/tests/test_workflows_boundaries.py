"""Boundary and concurrency regressions for the existing Workflows services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
from apps.channels.tasks import send_whatsapp_message_task
from apps.crm.models import AttributeDefinition, LeadReminder
from apps.followups.models import LeadSequenceState
from apps.hosted_automation.models import HostedAccountHealth
from apps.triggers.models import TriggerRun
from apps.triggers.tasks import _dispatch, _dispatch_messages
from services.triggers.actions import deliver_email, execute, scheduled_at
from services.triggers.rules import validate

from .test_workflows_runtime_contract import configuration, queue_run, queued_message
from .test_workflows_runtime_contract import env as env


@pytest.mark.parametrize("field_type,value", [
    ("text", "Actual text"), ("numeric", "12.50"),
    ("date", "2026-10-02"), ("datetime", "2026-10-02T14:30:00+05:30"),
])
def test_each_typed_attribute_action(env, field_type, value):
    AttributeDefinition.objects.create(organization=env.org, key="typed", name="Typed", field_type=field_type)
    data = configuration(env, action="attribute")
    data["action"] = {"key": "typed", "value": value}
    run = queue_run(env, action="attribute", data=data)
    execute(run.id)
    run.refresh_from_db()
    env.lead.refresh_from_db()
    assert run.status == "completed", run.detail
    assert env.lead.attributes["typed"] == value
    assert env.lead.attributes["temperature"] == "Hot"


@pytest.mark.parametrize("field_type,value", [
    ("numeric", "NaN"), ("numeric", "Infinity"), ("numeric", "hello"),
    ("date", "2026-02-30"), ("datetime", "not-a-date"),
])
def test_invalid_typed_action_is_rejected_before_execution(env, field_type, value):
    AttributeDefinition.objects.create(organization=env.org, key="typed", name="Typed", field_type=field_type)
    data = configuration(env, action="attribute")
    data["action"] = {"key": "typed", "value": value}
    with pytest.raises(ValidationError):
        validate(env.org, data)


@pytest.mark.parametrize("overwrite", [False, True])
def test_reminder_overwrite_is_explicit(env, overwrite):
    old = LeadReminder.objects.create(lead=env.lead, title="Existing", due_at=timezone.now())
    data = configuration(env, action="reminder")
    data["action"]["overwrite"] = overwrite
    run = queue_run(env, action="reminder", data=data)
    execute(run.id)
    old.refresh_from_db()
    run.refresh_from_db()
    assert old.status == ("cancelled" if overwrite else "pending")
    assert run.status == ("completed" if overwrite else "skipped")
    assert LeadReminder.objects.filter(lead=env.lead, status="pending").count() == 1


@pytest.mark.parametrize("action", ["ai", "followup"])
@pytest.mark.parametrize("enabled", [False, True])
def test_both_toggle_directions(env, action, enabled):
    data = configuration(env, action=action)
    data["action"]["enabled"] = enabled
    run = queue_run(env, action=action, data=data)
    execute(run.id)
    run.refresh_from_db()
    assert run.status == "completed", run.detail
    if action == "ai":
        env.lead.refresh_from_db()
        assert env.lead.ai_enabled is enabled
    else:
        assert LeadSequenceState.objects.get(lead=env.lead).lead_auto_followup_enabled is enabled


def test_datetime_schedule_uses_saved_lead_value(env):
    reference = timezone.now()
    expected = reference + timedelta(hours=4)
    env.lead.attributes["appointment"] = expected.isoformat()
    assert scheduled_at({"schedule": "attribute", "date_attribute": "appointment"}, env.lead, reference) == expected
    for invalid in (None, "invalid", (reference-timedelta(hours=1)).isoformat()):
        env.lead.attributes["appointment"] = invalid
        with pytest.raises(ValueError):
            scheduled_at({"schedule": "attribute", "date_attribute": "appointment"}, env.lead, reference)


def test_recovery_reuses_same_message_after_lost_publication(env):
    run = queued_message(env)
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay", side_effect=RuntimeError("broker unavailable")):
        _dispatch_messages()
    run.refresh_from_db()
    assert run.status == "queued" and run.due_at > timezone.now()
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send:
        _dispatch_messages()
        send.assert_not_called()
        TriggerRun.objects.filter(pk=run.pk).update(due_at=timezone.now()-timedelta(seconds=1))
        _dispatch_messages()
        send.assert_called_once_with(str(run.message_id))
    assert WhatsAppMessage.objects.filter(lead=env.lead, direction="outbound").count() == 1


def test_stale_inflight_whatsapp_is_not_resent(env):
    run = queued_message(env)
    WhatsAppMessage.objects.filter(pk=run.message_id).update(status="sending", updated_at=timezone.now()-timedelta(minutes=11))
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send:
        _dispatch_messages()
    send.assert_not_called()
    run.refresh_from_db()
    assert run.status == "needs_review" and run.finished_at is not None


def test_stale_email_claim_is_reviewed_without_resend(env):
    run = queue_run(env, action="email")
    execute(run.id)
    TriggerRun.objects.filter(pk=run.pk).update(status="sending", due_at=timezone.now()-timedelta(minutes=11))
    with patch("services.triggers.actions.send_organization_email") as send:
        _dispatch()
    send.assert_not_called()
    run.refresh_from_db()
    assert run.status == "needs_review"


def test_hosted_health_pause_defers_same_workflow_message(env):
    env.account.connection_type = "hosted"
    env.account.save(update_fields=["connection_type"])
    run = queued_message(env)
    until = timezone.now()+timedelta(hours=12)
    HostedAccountHealth.objects.update_or_create(account=env.account, defaults={"enabled": True, "paused_until": until})
    with patch("services.channels.whatsapp_service.send_outbound_message") as provider:
        result = send_whatsapp_message_task.run(str(run.message_id))
    provider.assert_not_called()
    run.refresh_from_db()
    assert result["status"] == "deferred"
    assert run.status == "queued" and run.due_at == until
    assert WhatsAppMessage.objects.get(pk=run.message_id).status == "queued"
    with patch("apps.channels.tasks.send_whatsapp_message_task.delay") as send:
        _dispatch_messages()
    send.assert_not_called()


def concurrently(function, run_id):
    barrier = Barrier(2)
    def perform():
        try:
            barrier.wait(timeout=10)
            function(run_id)
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(perform) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)


@pytest.mark.django_db(transaction=True)
def test_concurrent_execution_creates_one_reminder(env):
    run = queue_run(env, action="reminder")
    concurrently(execute, run.id)
    assert LeadReminder.objects.filter(lead=env.lead).count() == 1
    run.refresh_from_db()
    assert run.status == "completed"


@pytest.mark.django_db(transaction=True)
def test_concurrent_email_claim_sends_once(env):
    run = queue_run(env, action="email")
    execute(run.id)
    with patch("services.triggers.actions.send_organization_email", return_value=1) as send:
        concurrently(deliver_email, run.id)
    send.assert_called_once()
    run.refresh_from_db()
    assert run.status == "completed"
