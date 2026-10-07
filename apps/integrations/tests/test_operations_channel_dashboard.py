"""Channel authoring contracts: real models/services, providers always mocked."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.instagram_models import InstagramAccount
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Pipeline
from apps.followups.models import FollowupSequence, FollowupStep, LeadSequenceState
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.integrations.operations_auth import OperationsIdentity
from apps.integrations.operations_approval import approval_fingerprint
from apps.integrations.operations_models import OperationsAuditEvent, OperationsPolicy
from apps.integrations.operations_policy import (
    CAP_CADENCE_CONFIG_WRITE, CAP_CHANNEL_GROUP_READ, CAP_CHANNEL_GROUP_SEND, CAP_DIAGNOSTICS_READ,
    CAP_MESSAGING_CONFIG_WRITE, CAP_ORGANIZATION_READ, ROLE_ORGANIZATION_ADMIN,
)
from apps.integrations.operations.tools import channel_dashboard as channel
from apps.integrations.operations.tools.cadence import update_cadence_step
from apps.integrations.operations_tools import OperationsApprovalRequired, OperationsPermissionError, OperationsToolError
from apps.organizations.models import Organization


class ChannelDashboardTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Channel Operations")
        self.actor = User.objects.create_user(email="channel@example.test", password=None, name="Gaurav",
                                              organization=self.org, role=User.Role.ADMIN)
        self.caps = {CAP_ORGANIZATION_READ, CAP_DIAGNOSTICS_READ, CAP_CADENCE_CONFIG_WRITE, CAP_MESSAGING_CONFIG_WRITE,
                     CAP_CHANNEL_GROUP_READ, CAP_CHANNEL_GROUP_SEND}
        OperationsPolicy.objects.create(organization=self.org, organization_admin_enabled=True,
                                         allowed_capabilities=list(self.caps), approval_required_capabilities=[])
        self.identity = OperationsIdentity(token=SimpleNamespace(), actor=self.actor, role=ROLE_ORGANIZATION_ADMIN,
                                           organization=self.org, active_organization=None,
                                           scopes=frozenset({"operations.read", "operations.write"}),
                                           granted_capabilities=frozenset(self.caps))
        self.api = WhatsAppAccount.objects.create(organization=self.org, connection_type="api", status="connected",
                                                  is_active=True, display_phone_number="+919111111111", phone_number_id="meta-1")
        self.coexistence = WhatsAppAccount.objects.create(organization=self.org, connection_type="api", status="connected",
                                                          is_active=True, display_phone_number="+919222222222", phone_number_id="meta-2")
        WhatsAppConnectionAttempt.objects.create(organization=self.org, account=self.coexistence,
                                                  status="connected", stage="coexistence_connected")
        self.hosted = WhatsAppAccount.objects.create(organization=self.org, connection_type="hosted", status="connected",
                                                     is_active=True, display_phone_number="+919333333333", hosted_gateway_shard="primary")
        self.instagram = InstagramAccount.objects.create(organization=self.org, ig_user_id="ig-channel-1", status="connected")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Ops owned", owner=self.actor,
                                                country_code="+91", phone_number="9333333333")

    def _create(self, account, kind, name=None, **fields):
        return channel.upsert_channel_cadence(identity=self.identity, arguments={
            "data": {"name": name or f"{kind} cadence", "channel": kind, "account_id": str(account.pk), **fields},
            "dry_run": False, "reason": "Configure the agreed customer follow-up journey",
        })

    def _step(self, sequence, data, *, dry=False):
        return channel.add_channel_cadence_step(identity=self.identity, arguments={
            "cadence_id": str(sequence.pk), "data": data, "dry_run": dry,
            "reason": "Prepare the approved customer follow-up message",
        })

    def _template(self, account=None, **values):
        return WhatsAppTemplate.objects.create(organization=self.org, account=account or self.api,
                                               name="approved_intro", body="Hi {{lead_name}}", status="approved",
                                               meta_template_id="meta-template-1", **values)

    def _receipt(self, name, arguments, result, capability):
        return OperationsAuditEvent.objects.create(
            actor=self.actor, organization=self.org, role=self.identity.role,
            tool_name=name, capability=capability, outcome="dry_run", reason=arguments["reason"],
            request_fingerprint=approval_fingerprint(arguments), change_summary=result.audit_summary,
        )

    def test_discovery_distinguishes_all_four_channels_without_credentials(self):
        result = channel.get_channel_authoring_schema(identity=self.identity, arguments={}).data
        self.assertEqual({r["channel"] for r in result["accounts"]}, {"api", "coexistence", "hosted", "instagram"})
        self.assertNotIn("access_token", str(result))

    def test_create_all_channels_without_enrollment_or_send(self):
        for account, kind in [(self.api, "api"), (self.coexistence, "coexistence"), (self.hosted, "hosted"), (self.instagram, "instagram")]:
            result = self._create(account, kind).data
            self.assertEqual(result["cadence"]["account_id"], str(account.id))
            self.assertEqual(result["cadence"]["channel"], kind)
            self.assertTrue(result["cadence"]["is_active"])
        self.assertEqual(FollowupSequence.objects.count(), 4)
        self.assertEqual(LeadSequenceState.objects.count(), 0)

    def test_coexistence_must_not_select_hosted_or_plain_api(self):
        for account in [self.hosted, self.api]:
            with self.assertRaises(OperationsToolError):
                self._create(account, "coexistence")
        self.assertEqual(FollowupSequence.objects.count(), 0)

    def test_hosted_sender_cannot_create_meta_templates_even_with_legacy_credentials(self):
        from apps.integrations.operations.tools.whatsapp import create_whatsapp_template
        self.hosted.waba_id = "legacy-meta-id"
        self.hosted.access_token = "legacy-provider-token"
        self.hosted.save(update_fields=["waba_id", "access_token"])
        with self.assertRaisesMessage(OperationsToolError, "never a Hosted sender"):
            create_whatsapp_template(identity=self.identity, arguments={
                "whatsapp_account_id": str(self.hosted.id), "name": "invalid_hosted_template", "body": "Welcome",
                "reason": "Review hosted message authoring restrictions",
            })
        self.assertFalse(WhatsAppTemplate.objects.exists())

    def test_corrupt_cross_tenant_cadence_sender_is_not_disclosed(self):
        other = Organization.objects.create(name="Other tenant")
        sender = WhatsAppAccount.objects.create(organization=other, connection_type="hosted")
        seq = FollowupSequence.objects.create(organization=self.org, whatsapp_account=sender, name="Corrupt reference")
        with self.assertRaises(OperationsToolError):
            channel.get_channel_cadence_configuration(identity=self.identity, arguments={"cadence_id": str(seq.id)})

    def test_tenant_sender_and_oauth_scope_are_enforced(self):
        other = Organization.objects.create(name="Different customer")
        foreign = WhatsAppAccount.objects.create(organization=other, connection_type="api", is_active=True, status="connected")
        with self.assertRaises(OperationsToolError):
            self._create(foreign, "api")
        denied = replace(self.identity, granted_capabilities=frozenset({CAP_ORGANIZATION_READ}))
        with self.assertRaises(OperationsPermissionError):
            channel.upsert_channel_cadence(identity=denied, arguments={"reason": "Create sales cadence"})

    def test_dry_run_writes_nothing(self):
        result = channel.upsert_channel_cadence(identity=self.identity, arguments={
            "data": {"name": "Instagram review", "channel": "instagram", "account_id": str(self.instagram.pk)},
            "reason": "Review the Instagram customer follow-up configuration",
        })
        self.assertEqual(result.data["status"], "DRY_RUN")
        self.assertFalse(FollowupSequence.objects.exists())

    def test_existing_sender_cannot_be_changed(self):
        result = self._create(self.api, "api").data
        with self.assertRaises(OperationsToolError):
            channel.upsert_channel_cadence(identity=self.identity, arguments={
                "cadence_id": result["cadence"]["id"], "data": {"channel": "coexistence", "account_id": str(self.coexistence.id)},
                "dry_run": False, "reason": "Change sender for customer journey",
            })

    def test_hosted_and_instagram_use_canonical_text_steps(self):
        for account, kind in [(self.hosted, "hosted"), (self.instagram, "instagram")]:
            self._create(account, kind)
            seq = FollowupSequence.objects.get(name=f"{kind} cadence")
            result = self._step(seq, {"type": "text", "title": "Welcome", "body": "Hi {{ lead_name }}"})
            step = FollowupStep.objects.get(pk=result.data["step_id"])
            if kind == "hosted":
                self.assertEqual(HostedFollowupStepConfig.objects.get(step=step).body, "Hi {{lead_name}}")
                self.assertIsNone(step.whatsapp_template_id)
            else:
                self.assertEqual(step.instagram_body, "Hi {{lead_name}}")

    def test_invalid_placeholders_and_instagram_length_are_rejected(self):
        self._create(self.instagram, "instagram")
        seq = FollowupSequence.objects.get()
        for text in ["Hi {{unknown_attribute}}", "Hi {{lead_name", "x" * 1001]:
            with self.assertRaises(OperationsToolError):
                self._step(seq, {"type": "text", "body": text})
        self.assertFalse(FollowupStep.objects.exists())

    def test_api_steps_require_sender_bound_approved_template(self):
        self._create(self.coexistence, "coexistence")
        seq = FollowupSequence.objects.get()
        wrong_sender = self._template()
        with self.assertRaises(OperationsToolError):
            self._step(seq, {"type": "template", "template_id": str(wrong_sender.pk)})
        with self.assertRaises(OperationsToolError):
            self._step(seq, {"type": "text", "body": "Hi"})
        correct = self._template(account=self.coexistence)
        step = self._step(seq, {"type": "template", "template_id": str(correct.id)})
        self.assertEqual(FollowupStep.objects.get(pk=step.data["step_id"]).whatsapp_template_id, correct.id)

    def test_numeric_meta_parameters_require_explicit_bindings(self):
        self._create(self.api, "api")
        seq = FollowupSequence.objects.get()
        template = self._template()
        WhatsAppTemplateMetadata.objects.create(template=template, components=[{"type": "BODY", "text": "Hi {{1}}"}])
        with self.assertRaises(OperationsToolError):
            self._step(seq, {"type": "template", "template_id": str(template.id)})
        result = channel.configure_whatsapp_template_delivery(identity=self.identity, arguments={
            "template_id": str(template.id), "bindings": {"body.1": {"source": "lead_name", "default": "there"}},
            "dry_run": False, "reason": "Bind customer name to the approved Meta template",
        })
        self.assertEqual(result.data["verification"], "passed")
        self._step(seq, {"type": "template", "template_id": str(template.id)})

    def test_template_schema_and_dry_run_do_not_create_metadata(self):
        template = self._template()
        result = channel.get_channel_authoring_schema(identity=self.identity, arguments={"template_id": str(template.pk)}).data
        self.assertEqual(result["template_delivery"]["fields"][0]["key"], "body.1")
        channel.configure_whatsapp_template_delivery(identity=self.identity, arguments={
            "template_id": str(template.id), "bindings": {"body.1": {"source": "lead_name", "default": ""}},
            "reason": "Review template customer-name mapping before applying",
        })
        self.assertFalse(WhatsAppTemplateMetadata.objects.exists())

    def test_instagram_step_update_stays_instagram(self):
        self._create(self.instagram, "instagram")
        seq = FollowupSequence.objects.get()
        step_result = self._step(seq, {"type": "text", "body": "Hello"})
        update_cadence_step(identity=self.identity, arguments={
            "cadence_id": str(seq.pk), "step_id": step_result.data["step_id"], "data": {"body": "Hello {{lead_name}}"},
            "dry_run": False, "reason": "Personalize the Instagram customer welcome message",
        })
        step = FollowupStep.objects.get()
        self.assertEqual(step.instagram_body, "Hello {{lead_name}}")
        self.assertEqual(step.reminder_text, "")

    def test_channel_cadence_read_paginates_full_typed_content(self):
        self._create(self.hosted, "hosted")
        seq = FollowupSequence.objects.get()
        body = "A reviewed customer message. " * 60
        self._step(seq, {"type": "text", "title": "Welcome", "body": body})
        self._step(seq, {"type": "text", "title": "Follow up", "body": "Can we help?"})
        result = channel.get_channel_cadence_configuration(identity=self.identity, arguments={"cadence_id": str(seq.id), "limit": 1}).data
        self.assertEqual(result["cadence"]["channel"], "hosted")
        self.assertEqual(result["steps"][0]["hosted_body"], body.strip())
        self.assertEqual(result["steps"][0]["content_truncated_fields"], [])
        self.assertEqual(result["next_offset"], 1)
        page = channel.get_channel_cadence_configuration(identity=self.identity, arguments={"cadence_id": str(seq.id), "limit": 1, "offset": 1}).data
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["steps"][0]["hosted_body"], "Can we help?")

    def test_instagram_simulation_uses_instagram_controls_and_body(self):
        from apps.integrations.operations.tools.simulations import simulate_cadence
        self._create(self.instagram, "instagram")
        seq = FollowupSequence.objects.get()
        self._step(seq, {"type": "text", "body": "Hi {{lead_name}}"})
        result = simulate_cadence(identity=self.identity, arguments={"cadence_id": str(seq.id)}).data
        self.assertEqual(result["steps"][0]["content_preview"], "Hi {{lead_name}}")
        self.assertEqual(result["messages_sent"], 0)

    @patch.object(channel, "gateway_client_for_account")
    def test_group_dry_run_does_not_contact_provider_and_binds_sender(self, gateway):
        args = {"whatsapp_account_id": str(self.hosted.id), "sender_member_id": str(self.actor.id),
                "group_id": "120363123456789@g.us", "body": "The setup is ready for review.",
                "reason": "Notify the client support group about the reviewed setup"}
        result = channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments=args)
        self.assertEqual(result.data["message"]["body"], "Gaurav: The setup is ready for review.")
        gateway.assert_not_called()
        receipt = self._receipt("send_hosted_whatsapp_group_message", args, result, CAP_CHANNEL_GROUP_SEND)
        gateway.return_value.send_group_message.return_value = {"ok": True, "messageId": "sent-1"}
        sent = channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments={
            **args, "dry_run": False, "approved": True, "approval_event_id": str(receipt.id),
        })
        self.assertEqual(sent.data["status"], "SENT")
        gateway.return_value.send_group_message.assert_called_once_with(
            session_id=self.hosted.id, group_id=args["group_id"], body=result.data["message"]["body"], request_id=str(receipt.id),
        )

    @patch.object(channel, "gateway_client_for_account")
    def test_group_approval_cannot_change_body_or_be_replayed(self, gateway):
        args = {"whatsapp_account_id": str(self.hosted.id), "sender_member_id": str(self.actor.id),
                "group_id": "120363123456789@g.us", "body": "Approved body", "reason": "Update the client about approved setup"}
        dry = channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments=args)
        receipt = self._receipt("send_hosted_whatsapp_group_message", args, dry, CAP_CHANNEL_GROUP_SEND)
        approved = {**args, "dry_run": False, "approved": True, "approval_event_id": str(receipt.id)}
        with self.assertRaises(OperationsApprovalRequired):
            channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments={**approved, "body": "Different body"})
        gateway.assert_not_called()
        gateway.return_value.send_group_message.return_value = {"ok": True, "messageId": "sent-1"}
        channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments=approved)
        with self.assertRaises(OperationsApprovalRequired):
            channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments=approved)
        self.assertEqual(gateway.return_value.send_group_message.call_count, 1)

    @patch.object(channel, "gateway_client_for_account")
    def test_group_wrong_owner_and_direct_recipient_rejected(self, gateway):
        member = User.objects.create_user(email="other@example.test", password=None, name="Other", organization=self.org)
        args = {"whatsapp_account_id": str(self.hosted.id), "sender_member_id": str(self.actor.id),
                "group_id": "120363123456789@g.us", "body": "Hello", "reason": "Review the operations support message"}
        for changes in [{"sender_member_id": str(member.id)}, {"group_id": "919876543210@c.us"}]:
            with self.assertRaises(OperationsToolError):
                channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments={**args, **changes})
        gateway.assert_not_called()

    @override_settings(APP_ENV="staging", OUTBOUND_MESSAGING_ENABLED=True, STAGING_ALLOWED_RECIPIENTS={"120363123456789"})
    @patch.object(channel, "gateway_client_for_account")
    def test_staging_group_requires_exact_group_allowlist_not_phone_digits(self, gateway):
        args = {"whatsapp_account_id": str(self.hosted.id), "sender_member_id": str(self.actor.id),
                "group_id": "120363123456789@g.us", "body": "Hello", "reason": "Review the staging group test message"}
        dry = channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments=args)
        receipt = self._receipt("send_hosted_whatsapp_group_message", args, dry, CAP_CHANNEL_GROUP_SEND)
        with self.assertRaises(OperationsToolError):
            channel.send_hosted_whatsapp_group_message(identity=self.identity, arguments={
                **args, "dry_run": False, "approved": True, "approval_event_id": str(receipt.id),
            })
        gateway.assert_not_called()

    @patch.object(channel, "gateway_client_for_account")
    def test_group_reads_preserve_long_content_and_require_dedicated_scope(self, gateway):
        gateway.return_value.read_group.return_value = {"messages": [{"body": "word " * 400}]}
        args = {"whatsapp_account_id": str(self.hosted.id), "group_id": "120363123456789@g.us"}
        result = channel.read_hosted_whatsapp_group(identity=self.identity, arguments=args)
        self.assertEqual(len(result.data["result"]["messages"][0]["body"]), 2000)
        with self.assertRaises(OperationsPermissionError):
            channel.read_hosted_whatsapp_group(identity=replace(self.identity, granted_capabilities=frozenset({CAP_ORGANIZATION_READ})), arguments=args)
