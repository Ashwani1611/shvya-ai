import copy
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead, LeadCall, LeadReminder, Pipeline
from apps.followups.models import FollowupSequence, FollowupStep, LeadSequenceState
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun
from services.triggers.actions import deliver_email, execute, scheduled_at
from services.triggers.evaluator import emit, evaluate, scan_timers
from services.triggers.rules import catalog, reorder, save_rule, validate


class SmartTriggerTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Smart Triggers Test", timezone="Asia/Kolkata"
        )
        self.user = User.objects.create_user(
            email="trigger-admin@example.com",
            password="test-pass",
            organization=self.org,
            role="admin",
            name="Admin",
        )
        self.pipeline = self.org.pipelines.first()
        self.pipeline.phone_number = "+919999999999"
        self.pipeline.save()
        self.stage = self.pipeline.stages.order_by("display_order").first()
        self.other_stage = self.pipeline.stages.order_by("display_order")[1]
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Sender",
            display_phone_number="+919999999999",
            phone_number_id="123",
            status="connected",
            is_active=True,
        )
        self.sequence = FollowupSequence.objects.create(
            organization=self.org,
            name="Nurture",
            whatsapp_account=self.account,
            created_by=self.user,
        )
        FollowupStep.objects.create(
            sequence=self.sequence,
            position=1,
            step_type="reminder",
            reminder_text="Follow up",
            schedule_type="delay",
            delay_value=1,
            delay_unit="days",
        )
        self.attribute = AttributeDefinition.objects.create(
            organization=self.org,
            key="temperature",
            name="Temperature",
            field_type="option",
            options=["Hot", "Warm"],
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Test Lead",
            phone="+919111111111",
            email="lead@example.com",
            attributes={"temperature": "Hot"},
        )
        self.data = {
            "name": "New lead AI",
            "enabled": True,
            "trigger_type": "lead_created",
            "conditions": {
                "scopes": [
                    {"pipeline": str(self.pipeline.id), "stages": [str(self.stage.id)]}
                ],
                "attributes": [],
            },
            "action_type": "ai",
            "action": {"enabled": False},
        }

    def rule(self, **changes):
        data = copy.deepcopy(self.data)
        data.update(changes)
        return save_rule(self.user, data)

    def fire(self, rule, payload=None):
        event = emit(
            self.lead,
            rule.trigger_type,
            f"test:{TriggerEvent.objects.count()}",
            payload,
        )
        evaluate(event.id)
        run = TriggerRun.objects.get(rule=rule, event=event)
        execute(run.id)
        run.refresh_from_db()
        return run

    def authenticate(self, user=None):
        session = SessionStore()
        set_authenticated_user(session, user or self.user)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def test_duplicate_is_semantic_and_ignores_name(self):
        self.rule()
        with self.assertRaises(ValidationError):
            self.rule(name="Different name")

    def test_foreign_stage_and_sequence_are_rejected(self):
        foreign = Organization.objects.create(name="Other")
        stage = foreign.pipelines.first().stages.first()
        data = copy.deepcopy(self.data)
        data["conditions"]["scopes"][0]["stages"] = [str(stage.id)]
        with self.assertRaises(ValidationError):
            validate(self.org, data)
        data = copy.deepcopy(self.data)
        data.update(action_type="start_sequence", action={"sequence": str(stage.id)})
        with self.assertRaises(ValidationError):
            validate(self.org, data)
        self.assertNotIn(stage.id, [s["id"] for s in catalog(self.org)["stages"]])

    def test_empty_scopes_and_wrong_attribute_type_fail(self):
        data = copy.deepcopy(self.data)
        data["conditions"]["scopes"] = []
        with self.assertRaises(ValidationError):
            validate(self.org, data)
        data = copy.deepcopy(self.data)
        data.update(
            action_type="attribute", action={"key": "temperature", "value": "Invalid"}
        )
        with self.assertRaises(ValidationError):
            validate(self.org, data)

    def test_idempotent_event_and_cooldown(self):
        rule = self.rule()
        run = self.fire(rule)
        self.assertEqual(run.status, "completed")
        evaluate(run.event_id)
        execute(run.id)
        self.assertEqual(TriggerRun.objects.filter(event=run.event).count(), 1)
        self.assertEqual(self.fire(rule).status, "skipped")
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.ai_enabled)

    def test_new_rule_does_not_replay_old_events(self):
        old = TriggerEvent.objects.get(key=f"lead-created:{self.lead.id}")
        self.rule()
        evaluate(old.id)
        self.assertFalse(TriggerRun.objects.filter(event=old).exists())

    def test_attribute_conditions_all_and_values_any(self):
        data = copy.deepcopy(self.data["conditions"])
        data["attributes"] = [
            {"key": "temperature", "match": "equals", "values": ["Warm", "HOT"]}
        ]
        self.assertEqual(self.fire(self.rule(conditions=data)).status, "completed")

    def test_keyword_and_manual_call_events(self):
        c = copy.deepcopy(self.data["conditions"])
        c["keywords"] = ["price"]
        rule = self.rule(trigger_type="keyword", conditions=c)
        event = emit(
            self.lead, "keyword", "keyword-test", {"body": "What is your PRICE?"}
        )
        evaluate(event.id)
        self.assertTrue(TriggerRun.objects.filter(rule=rule, event=event).exists())
        c = copy.deepcopy(self.data["conditions"])
        c["call_status"] = "completed"
        rule = self.rule(trigger_type="call_logged", conditions=c)
        LeadCall.objects.create(
            lead=self.lead, user=self.user, status="completed", called_at=timezone.now()
        )
        event = TriggerEvent.objects.filter(kind="call_logged").latest("created_at")
        evaluate(event.id)
        self.assertTrue(TriggerRun.objects.filter(rule=rule, event=event).exists())
        LeadCall.objects.create(
            lead=self.lead, status="completed", called_at=timezone.now()
        )
        self.assertEqual(TriggerEvent.objects.filter(kind="call_logged").count(), 1)

    def test_stage_snapshot_survives_a_later_move(self):
        rule = self.rule(trigger_type="stage_moved")
        event = emit(self.lead, "stage_moved", "stage-snapshot")
        self.lead.stage = self.other_stage
        self.lead.save(update_fields=["stage"])
        evaluate(event.id)
        self.assertTrue(TriggerRun.objects.filter(rule=rule, event=event).exists())

    def test_signal_does_not_emit_for_unsaved_stage(self):
        self.lead.stage = self.other_stage
        self.lead.name = "Updated name"
        self.lead.save(update_fields=["name"])
        self.assertFalse(TriggerEvent.objects.filter(kind="stage_moved").exists())

    def test_signal_rolls_back_with_transaction(self):
        before = TriggerEvent.objects.count()
        try:
            with transaction.atomic():
                self.lead.stage = self.other_stage
                self.lead.save()
                raise ValueError("rollback")
        except ValueError:
            pass
        self.assertEqual(TriggerEvent.objects.count(), before)

    def test_stage_timer_once_per_entry_and_cancelled_after_move(self):
        c = copy.deepcopy(self.data["conditions"])
        c.update(duration=1, unit="minutes")
        rule = self.rule(trigger_type="stage_idle", conditions=c)
        Lead.objects.filter(id=self.lead.id).update(
            stage_entered_at=timezone.now() - timedelta(minutes=2)
        )
        scan_timers()
        scan_timers()
        self.assertEqual(TriggerEvent.objects.filter(kind="stage_idle").count(), 1)
        event = TriggerEvent.objects.get(kind="stage_idle")
        self.lead.stage = self.other_stage
        self.lead.save(update_fields=["stage"])
        evaluate(event.id)
        self.assertFalse(TriggerRun.objects.filter(rule=rule).exists())

    def test_no_response_cancelled_by_reply(self):
        c = copy.deepcopy(self.data["conditions"])
        c.update(duration=1, unit="minutes")
        rule = self.rule(trigger_type="no_response", conditions=c)
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="outbound",
            status="sent",
            body="Hello",
        )
        TriggerEvent.objects.filter(key=f"outbound-sent:{message.id}").update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        scan_timers()
        event = TriggerEvent.objects.get(kind="no_response")
        WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="inbound",
            status="received",
            body="Hi",
        )
        evaluate(event.id)
        self.assertFalse(TriggerRun.objects.filter(rule=rule).exists())

    def test_sequence_actions_reuse_existing_controls(self):
        start = self.rule(
            action_type="start_sequence",
            action={"sequence": str(self.sequence.id), "replace": False},
        )
        run = self.fire(start)
        self.assertEqual(run.status, "completed", run.detail)
        state = LeadSequenceState.objects.get(lead=self.lead)
        self.assertEqual(state.sequence, self.sequence)
        toggle = self.rule(action_type="followup", action={"enabled": False})
        self.assertEqual(self.fire(toggle).status, "completed")
        state.refresh_from_db()
        self.assertFalse(state.lead_auto_followup_enabled)
        stop = self.rule(action_type="stop_sequence", action={})
        self.assertEqual(self.fire(stop).status, "completed")
        state.refresh_from_db()
        self.assertEqual(state.status, "cleared")

    def test_sequence_completion_signal(self):
        c = copy.deepcopy(self.data["conditions"])
        c["sequences"] = [str(self.sequence.id)]
        rule = self.rule(trigger_type="sequence_ended", conditions=c)
        state = LeadSequenceState.objects.create(
            organization=self.org, lead=self.lead, sequence=self.sequence
        )
        state.status = "completed"
        state.completed_at = timezone.now()
        state.save()
        event = TriggerEvent.objects.get(kind="sequence_ended")
        evaluate(event.id)
        self.assertTrue(TriggerRun.objects.filter(rule=rule, event=event).exists())
        state.save()
        self.assertEqual(TriggerEvent.objects.filter(kind="sequence_ended").count(), 1)

    def test_reminder_and_typed_attribute_actions(self):
        reminder = self.rule(
            action_type="reminder",
            action={
                "duration": 2,
                "unit": "hours",
                "note": "Call {{lead_name}}",
                "overwrite": False,
            },
        )
        run = self.fire(reminder)
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            LeadReminder.objects.get(lead=self.lead).description, "Call Test Lead"
        )
        attr = self.rule(
            action_type="attribute", action={"key": "temperature", "value": "Warm"}
        )
        self.assertEqual(self.fire(attr).status, "completed")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["temperature"], "Warm")

    def test_cross_pipeline_move_records_real_crm_change(self):
        pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        stage = pipeline.stages.first()
        rule = self.rule(
            action_type="move_stage",
            action={"pipeline": str(pipeline.id), "stage": str(stage.id)},
        )
        self.assertEqual(self.fire(rule).status, "completed")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline, pipeline)
        self.assertTrue(TriggerEvent.objects.filter(kind="stage_moved").exists())

    def test_fixed_schedule_uses_organization_timezone(self):
        due = scheduled_at(
            {"schedule": "fixed", "time": "09:30"},
            self.lead,
            datetime(2026, 9, 6, 10, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(
            due.astimezone(dt_timezone.utc),
            datetime(2026, 9, 7, 4, 0, tzinfo=dt_timezone.utc),
        )

    def test_whatsapp_is_scheduled_then_blocked_outside_reply_window(self):
        rule = self.rule(
            action_type="message",
            action={
                "account": str(self.account.id),
                "body": "Hello {{lead_name}}",
                "schedule": "relative",
                "duration": 0,
                "unit": "minutes",
            },
        )
        run = self.fire(rule)
        self.assertEqual(run.status, "scheduled")
        execute(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")

    def test_whatsapp_queues_once_in_reply_window(self):
        WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="inbound",
            status="received",
            body="Hi",
        )
        rule = self.rule(
            action_type="message",
            action={
                "account": str(self.account.id),
                "body": "Hello {{lead_name}}",
                "schedule": "relative",
                "duration": 0,
                "unit": "minutes",
            },
        )
        run = self.fire(rule)
        execute(run.id)
        execute(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.message.body, "Hello Test Lead")
        self.assertEqual(
            WhatsAppMessage.objects.filter(direction="outbound").count(), 1
        )

    @override_settings(FOLLOWUP_EMAIL_DELIVERY_ENABLED=True)
    def test_email_has_durable_claim_and_no_duplicate(self):
        rule = self.rule(
            action_type="email",
            action={"subject": "Hello {{lead_name}}", "body": "Welcome"},
        )
        run = self.fire(rule)
        self.assertEqual(run.status, "email_ready")
        deliver_email(run.id)
        deliver_email(run.id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(run.id), mail.outbox[0].extra_headers["Message-ID"])

    def test_disabled_rule_cancels_pending_action(self):
        rule = self.rule()
        event = emit(self.lead, "lead_created", "disable-test")
        evaluate(event.id)
        rule.enabled = False
        rule.save()
        run = TriggerRun.objects.get(event=event)
        execute(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, "skipped")

    def test_reorder_exact_membership_and_execution_order(self):
        first = self.rule()
        second = self.rule(action={"enabled": True})
        reorder(self.user, [str(second.id), str(first.id)])
        self.assertEqual(
            list(SmartTrigger.objects.values_list("id", flat=True)),
            [second.id, first.id],
        )
        with self.assertRaises(ValidationError):
            reorder(self.user, [str(first.id), str(first.id)])
        from apps.triggers.tasks import dispatch_smart_triggers

        emit(self.lead, "lead_created", "ordered-event")
        dispatch_smart_triggers()
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.ai_enabled)

    def test_dashboard_crud_and_member_read_only(self):
        self.authenticate()
        self.assertEqual(
            self.client.get(reverse("crm-smart-triggers")).status_code, 200
        )
        url = reverse("smart-trigger-rules")
        response = self.client.post(
            url, json.dumps(self.data), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.client.get(url).json()["rules"]), 1)
        self.user.role = "agent"
        self.user.save()
        self.authenticate()
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            self.client.post(
                url, json.dumps(self.data), content_type="application/json"
            ).status_code,
            403,
        )

    def test_http_foreign_rule_inaccessible_and_csrf_required(self):
        rule = self.rule()
        other = Organization.objects.create(name="Foreign")
        user = User.objects.create_user(
            email="other-trigger@example.com",
            organization=other,
            role="admin",
            password="test",
        )
        self.authenticate(user)
        response = self.client.delete(reverse("smart-trigger-rule", args=[rule.id]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.get(reverse("smart-trigger-rules")).json()["rules"], []
        )

    def test_invalid_body_is_400(self):
        self.authenticate()
        for body in ["[]", "null", "{bad", '{"trigger_type": []}']:
            response = self.client.post(
                reverse("smart-trigger-rules"), body, content_type="application/json"
            )
            self.assertEqual(response.status_code, 400)

    def test_enabling_does_not_replay_events_while_disabled(self):
        rule = self.rule(enabled=False)
        event = emit(self.lead, "lead_created", "disabled-at-event")
        rule.enabled = True
        rule.save()
        evaluate(event.id)
        self.assertFalse(TriggerRun.objects.filter(rule=rule).exists())

    def test_causal_chain_excludes_previously_executed_rule(self):
        from services.triggers.evaluator import causal_rules

        rule = self.rule()
        token = causal_rules.set((str(rule.id),))
        try:
            event = emit(self.lead, "lead_created", "causal-loop")
        finally:
            causal_rules.reset(token)
        evaluate(event.id)
        self.assertFalse(TriggerRun.objects.filter(rule=rule).exists())

    def test_queued_time_does_not_start_no_response_timer(self):
        c = copy.deepcopy(self.data["conditions"])
        c.update(duration=1, unit="minutes")
        self.rule(trigger_type="no_response", conditions=c)
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="outbound",
            status="queued",
        )
        WhatsAppMessage.objects.filter(id=message.id).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        scan_timers()
        self.assertFalse(TriggerEvent.objects.filter(kind="no_response").exists())
        message.status = "sent"
        message.save(update_fields=["status"])
        scan_timers()
        self.assertFalse(TriggerEvent.objects.filter(kind="no_response").exists())
        clock = TriggerEvent.objects.get(key=f"outbound-sent:{message.id}")
        original = clock.created_at
        message.status = "delivered"
        message.save(update_fields=["status"])
        clock.refresh_from_db()
        self.assertEqual(clock.created_at, original)

    def test_reply_after_evaluation_cancels_pending_action(self):
        c = copy.deepcopy(self.data["conditions"])
        c.update(duration=1, unit="minutes")
        rule = self.rule(trigger_type="no_response", conditions=c)
        message = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="outbound",
            status="sent",
        )
        TriggerEvent.objects.filter(key=f"outbound-sent:{message.id}").update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        scan_timers()
        event = TriggerEvent.objects.get(kind="no_response")
        evaluate(event.id)
        run = TriggerRun.objects.get(rule=rule)
        WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=self.lead,
            direction="inbound",
            status="received",
            body="Replied",
        )
        execute(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, "skipped")
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.ai_enabled)

    def test_authenticated_write_requires_csrf(self):
        self.authenticate()
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        response = client.post(
            reverse("smart-trigger-rules"),
            json.dumps(self.data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_wildcard_keyword_matches_any_inbound_text(self):
        c = copy.deepcopy(self.data["conditions"])
        c["keywords"] = ["*"]
        rule = self.rule(trigger_type="keyword", conditions=c)
        self.assertEqual(self.fire(rule, {"body": "Anything"}).status, "completed")
