from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.engagement import (
    EngagementDecision,
)
from apps.channels.models import (
    WhatsAppAccount,
    WhatsAppMessage,
)
from apps.crm.models import (
    Lead,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class WhatsAppEngagementTriggerTests(TestCase):
    """
    Tests the WhatsApp -> AI Engagement Celery integration.

    External systems are mocked.

    These tests do NOT call:
        - OpenAI
        - Meta
        - a real Celery worker
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="WhatsApp Engagement Test Organization",
        )

        cls.other_organization = Organization.objects.create(
            name="Other WhatsApp Organization",
        )

        cls.org_info = OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification programs."
            ),
            bot_languages="English, Hindi, Hinglish",
            qualification_requirements=(
                "Identify course interest, budget, "
                "timeline, and buying intent."
            ),
            engagement_instructions=(
                "Use a friendly, concise, and helpful tone."
            ),
            ai_enabled=True,
            bump_up_enabled=True,
            bump_up_count=2,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="WhatsApp Engagement Pipeline",
            description="Pipeline used for WhatsApp engagement tests.",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New Lead",
            description="New incoming lead.",
            display_order=0,
            is_active=True,
            ai_on=True,
        )

        cls.other_pipeline = Pipeline.objects.create(
            organization=cls.other_organization,
            name="Other Pipeline",
            description="Other organization pipeline.",
            is_active=True,
        )

        cls.other_stage = Stage.objects.create(
            pipeline=cls.other_pipeline,
            name="New Lead",
            description="Other organization stage.",
            display_order=0,
            is_active=True,
            ai_on=True,
        )

        cls.account = WhatsAppAccount.objects.create(
            organization=cls.organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="WhatsApp Engagement Test",
            status=(
                WhatsAppAccount.Status.CONNECTED
            ),
            is_active=True,
        )

        cls.other_account = WhatsAppAccount.objects.create(
            organization=cls.other_organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="Other Organization WhatsApp",
            status=(
                WhatsAppAccount.Status.CONNECTED
            ),
            is_active=True,
        )

    # ========================================================
    # HELPERS
    # ========================================================

    def create_lead(
        self,
        *,
        phone="+919876543210",
        name="WhatsApp Engagement Lead",
        organization=None,
        pipeline=None,
        stage=None,
        ai_enabled=True,
    ):
        organization = (
            organization
            or self.organization
        )
        pipeline = (
            pipeline
            or self.pipeline
        )
        stage = (
            stage
            or self.stage
        )

        return Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name=name,
            phone=phone,
            email="lead@example.com",
            notes="",
            attributes={},
            lead_source="whatsapp_api",
            ai_enabled=ai_enabled,
        )

    def create_message(
        self,
        *,
        lead,
        account=None,
        external_id="wamid-test-001",
        body="I am interested in the course.",
    ):
        account = (
            account
            or self.account
        )

        return WhatsAppMessage.objects.create(
            organization=lead.organization,
            account=account,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.INBOUND
            ),
            external_id=external_id,
            from_number=lead.phone,
            to_number="919999999999",
            body=body,
            status=(
                WhatsAppMessage.Status.RECEIVED
            ),
            raw_payload={
                "test": True,
            },
            is_read=False,
        )

    def engagement_decision(
        self,
        *,
        should_engage=True,
        message="Thanks for your interest!",
        crm_actions=None,
        reason="Customer response is appropriate.",
    ):
        return EngagementDecision(
            should_engage=should_engage,
            message=message if should_engage else "",
            file_document_id=None,
            crm_actions=crm_actions or [],
            reason=reason,
            model="gpt-4.1-nano",
        )

    # ========================================================
    # INBOUND TRIGGER
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.process_whatsapp_engagement.delay"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_inbound_message_queues_engagement_after_commit(
        self,
        summary_delay,
        engagement_delay,
    ):
        """
        A successful inbound WhatsApp message must queue both
        internal summary and AI engagement after the transaction
        commits.
        """
        lead = self.create_lead(
            phone="+919876543201",
        )

        from services.channels.whatsapp_service import (
            handle_inbound_message,
        )

        handle_inbound_message(
            organization=self.organization,
            account=self.account,
            external_id="wamid-trigger-001",
            from_number=lead.phone,
            to_number="919999999999",
            body="Yes, I am interested.",
            raw_payload={
                "test": True,
            },
        )

        summary_delay.assert_called_once_with(
            str(lead.id),
        )

        engagement_delay.assert_called_once_with(
            str(lead.id),
            str(self.account.id),
        )

    # ========================================================
    # LEAD ID + ACCOUNT ID
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.process_whatsapp_engagement.delay"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_engagement_trigger_receives_lead_and_account_ids(
        self,
        summary_delay,
        engagement_delay,
    ):
        """
        The trigger must pass the exact Lead and WhatsAppAccount
        IDs. It must not resolve a different account later.
        """
        lead = self.create_lead(
            phone="+919876543202",
        )

        from services.channels.whatsapp_service import (
            handle_inbound_message,
        )

        handle_inbound_message(
            organization=self.organization,
            account=self.account,
            external_id="wamid-trigger-002",
            from_number=lead.phone,
            to_number="919999999999",
            body="Tell me more.",
            raw_payload={
                "test": True,
            },
        )

        engagement_delay.assert_called_once_with(
            str(lead.id),
            str(self.account.id),
        )

    # ========================================================
    # IDEMPOTENCY
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.process_whatsapp_engagement.delay"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_duplicate_inbound_external_id_does_not_queue_again(
        self,
        summary_delay,
        engagement_delay,
    ):
        """
        Meta webhook retries must not create another engagement
        job for the same external_id.
        """
        lead = self.create_lead(
            phone="+919876543203",
        )

        from services.channels.whatsapp_service import (
            handle_inbound_message,
        )

        kwargs = {
            "organization": self.organization,
            "account": self.account,
            "external_id": "wamid-trigger-duplicate",
            "from_number": lead.phone,
            "to_number": "919999999999",
            "body": "I am interested.",
            "raw_payload": {
                "test": True,
            },
        }

        handle_inbound_message(
            **kwargs,
        )

        handle_inbound_message(
            **kwargs,
        )

        self.assertEqual(
            WhatsAppMessage.objects.filter(
                external_id="wamid-trigger-duplicate",
            ).count(),
            1,
        )

        self.assertEqual(
            summary_delay.call_count,
            1,
        )

        self.assertEqual(
            engagement_delay.call_count,
            1,
        )

    # ========================================================
    # TASK: LEAD NOT FOUND
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_skips_when_lead_does_not_exist(
        self,
        engagement_service,
    ):
        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            "00000000-0000-0000-0000-000000000000",
            str(self.account.id),
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "lead_not_found",
        )

        engagement_service.assert_not_called()

    # ========================================================
    # TASK: ACCOUNT NOT FOUND
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_skips_when_account_does_not_exist(
        self,
        engagement_service,
    ):
        lead = self.create_lead(
            phone="+919876543204",
        )

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            str(lead.id),
            "00000000-0000-0000-0000-000000000000",
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "account_not_found",
        )

        engagement_service.assert_not_called()

    # ========================================================
    # TASK: ORGANIZATION ISOLATION
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_rejects_cross_organization_account(
        self,
        engagement_service,
    ):
        lead = self.create_lead(
            phone="+919876543205",
        )

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            str(lead.id),
            str(self.other_account.id),
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "organization_mismatch",
        )

        engagement_service.assert_not_called()

    # ========================================================
    # TASK: ORGANIZATION AI GATE
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_skips_when_organization_ai_disabled(
        self,
        engagement_service,
    ):
        lead = self.create_lead(
            phone="+919876543206",
        )

        self.org_info.ai_enabled = False
        self.org_info.save(
            update_fields=[
                "ai_enabled",
                "updated_at",
            ]
        )

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            str(lead.id),
            str(self.account.id),
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "organization_ai_disabled",
        )

        engagement_service.assert_not_called()

        self.org_info.ai_enabled = True
        self.org_info.save(
            update_fields=[
                "ai_enabled",
                "updated_at",
            ]
        )

    # ========================================================
    # TASK: LEAD AI GATE
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_skips_when_lead_ai_disabled(
        self,
        engagement_service,
    ):
        lead = self.create_lead(
            phone="+919876543207",
            ai_enabled=False,
        )

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            str(lead.id),
            str(self.account.id),
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "lead_ai_disabled",
        )

        engagement_service.assert_not_called()

    # ========================================================
    # TASK: STAGE AI GATE
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_skips_when_stage_ai_disabled(
        self,
        engagement_service,
    ):
        self.stage.ai_on = False
        self.stage.save(
            update_fields=[
                "ai_on",
                "updated_at",
            ]
        )

        lead = self.create_lead(
            phone="+919876543208",
        )

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        result = process_whatsapp_engagement.run(
            str(lead.id),
            str(self.account.id),
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "stage_ai_disabled",
        )

        engagement_service.assert_not_called()

        self.stage.ai_on = True
        self.stage.save(
            update_fields=[
                "ai_on",
                "updated_at",
            ]
        )

    # ========================================================
    # TASK: AI DECISION
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.CRMActionExecutor"
    )
    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_calls_engagement_service(
        self,
        engagement_service_class,
        executor_class,
    ):
        lead = self.create_lead(
            phone="+919876543209",
        )

        decision = self.engagement_decision(
            message="Thanks for reaching out!",
        )

        engagement_service = Mock()
        engagement_service.engage.return_value = decision
        engagement_service_class.return_value = (
            engagement_service
        )

        executor = Mock()
        executor.execute.return_value = []
        executor_class.return_value = executor

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        with patch(
            "apps.ai_engagement.tasks.queue_outbound_message",
        ) as queue_outbound:
            outbound = WhatsAppMessage(
                organization=self.organization,
                account=self.account,
                lead=lead,
                direction=(
                    WhatsAppMessage.Direction.OUTBOUND
                ),
                to_number=lead.phone,
                body=decision.message,
                status=(
                    WhatsAppMessage.Status.QUEUED
                ),
            )

            queue_outbound.return_value = outbound

            with patch(
                "apps.ai_engagement.tasks."
                "send_whatsapp_message_task.delay"
            ):
                result = process_whatsapp_engagement.run(
                    str(lead.id),
                    str(self.account.id),
                )

        engagement_service.engage.assert_called_once_with(
            organization=self.organization,
            lead=lead,
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

    # ========================================================
    # TASK: NO ENGAGEMENT
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.CRMActionExecutor"
    )
    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_does_not_queue_outbound_when_no_engagement(
        self,
        engagement_service_class,
        executor_class,
    ):
        lead = self.create_lead(
            phone="+919876543211",
        )

        decision = self.engagement_decision(
            should_engage=False,
            message="",
            reason="No customer response required.",
        )

        engagement_service = Mock()
        engagement_service.engage.return_value = decision
        engagement_service_class.return_value = (
            engagement_service
        )

        executor = Mock()
        executor.execute.return_value = []
        executor_class.return_value = executor

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        with patch(
            "apps.ai_engagement.tasks.queue_outbound_message"
        ) as queue_outbound, patch(
            "apps.ai_engagement.tasks."
            "send_whatsapp_message_task.delay"
        ) as send_delay:
            result = process_whatsapp_engagement.run(
                str(lead.id),
                str(self.account.id),
            )

        queue_outbound.assert_not_called()
        send_delay.assert_not_called()

        self.assertEqual(
            result["status"],
            "completed",
        )

        self.assertEqual(
            result["reason"],
            "no_engagement",
        )

        executor.execute.assert_called_once_with(
            organization=self.organization,
            lead=lead,
            actions=[],
        )

    # ========================================================
    # TASK: CRM ACTION EXECUTION
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.CRMActionExecutor"
    )
    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_executes_crm_actions(
        self,
        engagement_service_class,
        executor_class,
    ):
        lead = self.create_lead(
            phone="+919876543212",
        )

        crm_actions = [
            {
                "type": "add_note",
                "note": "Lead requested more information.",
            },
        ]

        decision = self.engagement_decision(
            message="Absolutely, I can help.",
            crm_actions=crm_actions,
        )

        engagement_service = Mock()
        engagement_service.engage.return_value = decision
        engagement_service_class.return_value = (
            engagement_service
        )

        executor = Mock()
        executor.execute.return_value = [
            {
                "type": "add_note",
                "status": "executed",
            },
        ]
        executor_class.return_value = executor

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        with patch(
            "apps.ai_engagement.tasks.queue_outbound_message"
        ) as queue_outbound:
            outbound = WhatsAppMessage(
                organization=self.organization,
                account=self.account,
                lead=lead,
                direction=(
                    WhatsAppMessage.Direction.OUTBOUND
                ),
                to_number=lead.phone,
                body=decision.message,
                status=(
                    WhatsAppMessage.Status.QUEUED
                ),
            )

            queue_outbound.return_value = outbound

            with patch(
                "apps.ai_engagement.tasks."
                "send_whatsapp_message_task.delay"
            ):
                result = process_whatsapp_engagement.run(
                    str(lead.id),
                    str(self.account.id),
                )

        executor.execute.assert_called_once_with(
            organization=self.organization,
            lead=lead,
            actions=crm_actions,
        )

        self.assertEqual(
            result["crm"],
            [
                {
                    "type": "add_note",
                    "status": "executed",
                },
            ],
        )

    # ========================================================
    # TASK: OUTBOUND QUEUE
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.CRMActionExecutor"
    )
    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_queues_customer_response(
        self,
        engagement_service_class,
        executor_class,
    ):
        lead = self.create_lead(
            phone="+919876543213",
        )

        decision = self.engagement_decision(
            message="Thanks for your interest!",
        )

        engagement_service = Mock()
        engagement_service.engage.return_value = decision
        engagement_service_class.return_value = (
            engagement_service
        )

        executor = Mock()
        executor.execute.return_value = []
        executor_class.return_value = executor

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        with patch(
            "apps.ai_engagement.tasks.queue_outbound_message"
        ) as queue_outbound:
            outbound = WhatsAppMessage(
                organization=self.organization,
                account=self.account,
                lead=lead,
                direction=(
                    WhatsAppMessage.Direction.OUTBOUND
                ),
                from_number=self.account.phone_number_id,
                to_number=lead.phone,
                body=decision.message,
                status=(
                    WhatsAppMessage.Status.QUEUED
                ),
            )

            outbound.id = (
                "11111111-1111-1111-1111-111111111111"
            )

            queue_outbound.return_value = outbound

            with patch(
                "apps.ai_engagement.tasks."
                "send_whatsapp_message_task.delay"
            ) as send_delay:
                result = process_whatsapp_engagement.run(
                    str(lead.id),
                    str(self.account.id),
                )

        queue_outbound.assert_called_once_with(
            organization=self.organization,
            account=self.account,
            to_number=lead.phone,
            body=decision.message,
            lead=lead,
        )

        send_delay.assert_called_once_with(
            str(outbound.id),
        )

        self.assertTrue(
            result["engaged"],
        )

        self.assertEqual(
            result["message_id"],
            str(outbound.id),
        )

    # ========================================================
    # TASK: CUSTOMER RESPONSE IS NEVER SENT INLINE
    # ========================================================

    @patch(
        "apps.ai_engagement.tasks.CRMActionExecutor"
    )
    @patch(
        "apps.ai_engagement.tasks.EngagementService"
    )
    def test_task_uses_existing_whatsapp_queue_and_sender(
        self,
        engagement_service_class,
        executor_class,
    ):
        """
        AI Engagement must not call Meta directly.

        It creates a queued WhatsAppMessage and hands the message
        ID to the existing sender task.
        """
        lead = self.create_lead(
            phone="+919876543214",
        )

        decision = self.engagement_decision(
            message="I can help you with that.",
        )

        engagement_service = Mock()
        engagement_service.engage.return_value = decision
        engagement_service_class.return_value = (
            engagement_service
        )

        executor = Mock()
        executor.execute.return_value = []
        executor_class.return_value = executor

        from apps.ai_engagement.tasks import (
            process_whatsapp_engagement,
        )

        with patch(
            "apps.ai_engagement.tasks.queue_outbound_message"
        ) as queue_outbound:
            outbound = WhatsAppMessage(
                organization=self.organization,
                account=self.account,
                lead=lead,
                direction=(
                    WhatsAppMessage.Direction.OUTBOUND
                ),
                from_number=self.account.phone_number_id,
                to_number=lead.phone,
                body=decision.message,
                status=(
                    WhatsAppMessage.Status.QUEUED
                ),
            )

            outbound.id = (
                "22222222-2222-2222-2222-222222222222"
            )

            queue_outbound.return_value = outbound

            with patch(
                "apps.ai_engagement.tasks."
                "send_whatsapp_message_task.delay"
            ) as send_delay:
                process_whatsapp_engagement.run(
                    str(lead.id),
                    str(self.account.id),
                )

        queue_outbound.assert_called_once()

        send_delay.assert_called_once_with(
            str(outbound.id),
        )