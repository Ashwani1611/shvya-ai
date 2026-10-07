"""Manual Hosted delivery must not wait for Celery or automation admission."""

import json
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError
from apps.organizations.models import Organization


class HostedManualImmediateTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.org = Organization.objects.create(
            name="Manual Hosted Org", package="dfy",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="hosted-manual@example.com", password="test-password",
            name="Manual Agent", organization=self.org, role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org, connection_type="hosted",
            phone_number_id="+919000000701", display_phone_number="+919000000701",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.gateway = self._patch("apps.channels.hosted_gateway_routing.gateway_client_for_account").return_value
        self.gateway.send_message.side_effect = self._ack
        self.gateway.send_uploaded_media.side_effect = self._ack
        self._patch("services.channels.hosted_health_guard.finalize_hosted_send")
        self._patch("services.channels.hosted_chat_service.queue_hosted_chat_refresh")
        self._patch("apps.channels.hosted_send_ui.queue_hosted_chat_refresh")
        self.admission = self._patch(
            "apps.core.fairness.admit_provider_start", return_value=(False, 60, "account"),
        )
        self.publish = self._patch("apps.channels.hosted_send_tasks.send_hosted_whatsapp_message_task.apply_async")
        self.retry = self._patch("apps.channels.hosted_send_tasks.send_hosted_whatsapp_message_task.retry")
        self.acks = 0

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _ack(self, **kwargs):
        self.assertFalse(transaction.get_connection().in_atomic_block)
        message = WhatsAppMessage.objects.get(pk=kwargs["request_id"])
        self.assertEqual(message.status, "sending")
        self.acks += 1
        return {"messageId": f"manual-{self.acks}"}

    def _post(self, chat="+919000000702", body="Manual reply", **extra):
        return self.client.post(
            reverse("whatsapp-hosted-session-chat-send", args=[self.account.id]),
            data=json.dumps({"chat": chat, "body": body, **extra}),
            content_type="application/json",
        )

    def _message(self, **changes):
        values = {
            "organization": self.org, "account": self.account,
            "direction": WhatsAppMessage.Direction.OUTBOUND,
            "from_number": self.account.display_phone_number,
            "to_number": "+919000000702", "body": "Manual reply",
            "status": WhatsAppMessage.Status.QUEUED,
            "raw_payload": {"shvya_hosted": {"origin": "agent"}},
        }
        values.update(changes)
        return WhatsAppMessage.objects.create(**values)

    def test_text_sends_before_response_without_worker_or_automation_admission(self):
        response = self._post()
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["retry_scheduled"])
        self.assertEqual(data["message"]["status"], "sent")
        message = WhatsAppMessage.objects.get(pk=data["message"]["id"])
        self.assertEqual(message.external_id, "wweb:manual-1")
        self.gateway.send_message.assert_called_once()
        self.publish.assert_not_called()
        self.admission.assert_not_called()

    def test_consecutive_manual_messages_do_not_wait_for_automation_limit(self):
        for index in range(3):
            self.assertEqual(self._post(body=f"Reply {index}").status_code, 201)
        self.assertEqual(self.gateway.send_message.call_count, 3)
        self.publish.assert_not_called()
        self.admission.assert_not_called()

    def test_manual_media_uses_same_immediate_path_and_cleans_confirmed_upload(self):
        for kind, filename, mime in (
            ("image", "photo.jpg", "image/jpeg"),
            ("video", "clip.mp4", "video/mp4"),
            ("document", "brochure.pdf", "application/pdf"),
        ):
            with self.subTest(kind=kind):
                response = self.client.post(
                    reverse("whatsapp-hosted-session-chat-media-send", args=[self.account.id]),
                    {"chat": "+919000000702", "message_type": kind,
                     "caption": "Attachment", "attachment": SimpleUploadedFile(filename, b"test-file", content_type=mime)},
                )
                self.assertEqual(response.status_code, 201)
                message = WhatsAppMessage.objects.get(pk=response.json()["message"]["id"])
                self.assertEqual(message.status, "sent")
                self.assertEqual(message.message_type, kind)
                self.assertFalse(default_storage.exists(message.media_payload["storage_path"]))
        self.assertEqual(self.gateway.send_uploaded_media.call_count, 3)
        self.publish.assert_not_called()
        self.admission.assert_not_called()

    def test_lid_is_preserved_as_transport_identity(self):
        response = self._post(chat="555555555555@lid")
        self.assertEqual(response.status_code, 201)
        message = WhatsAppMessage.objects.get(pk=response.json()["message"]["id"])
        self.assertEqual(message.to_number, "555555555555@lid")
        self.assertIsNone(message.lead_id)
        self.assertEqual(self.gateway.send_message.call_args.kwargs["to_number"], message.to_number)

    def test_group_uses_original_transport_identity(self):
        response = self._post(chat="120363000000000000@g.us")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.gateway.send_message.call_args.kwargs["to_number"], "120363000000000000@g.us")

    def test_legacy_worker_manual_message_also_skips_automation_admission(self):
        message = self._message()
        result = send_hosted_whatsapp_message_task.run(str(message.id))
        self.assertEqual(result["status"], "sent")
        self.admission.assert_not_called()
        self.publish.assert_not_called()

    def test_automation_cannot_opt_into_immediate_delivery(self):
        for marker in ("shvya_ai", "shvya_welcome", "shvya_auto_followup", "shvya_workflow", "shvya_sales"):
            with self.subTest(marker=marker):
                message = self._message(raw_payload={"shvya_hosted": {"origin": "agent"}, marker: {}})
                result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
                self.assertEqual(result["reason"], "not_manual")
                message.refresh_from_db()
                self.assertEqual(message.status, "queued")
        self.gateway.send_message.assert_not_called()

    def test_unlabelled_or_inbound_message_cannot_use_immediate_delivery(self):
        for changes in ({"raw_payload": {}}, {"direction": WhatsAppMessage.Direction.INBOUND}):
            with self.subTest(changes=changes):
                message = self._message(**changes)
                result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
                self.assertEqual(result["reason"], "not_manual")
        self.gateway.send_message.assert_not_called()

    def test_inflight_and_confirmed_messages_are_not_resent(self):
        for status in ("sending", "sent", "delivered", "read"):
            with self.subTest(status=status):
                message = self._message(status=status)
                result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
                self.assertEqual(result["status"], "skipped")
        self.gateway.send_message.assert_not_called()
        self.publish.assert_not_called()

    def test_second_attempt_of_same_row_does_not_duplicate_provider_send(self):
        message = self._message()
        send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        self.assertEqual(result["reason"], "already_sent")
        self.gateway.send_message.assert_called_once()

    def test_disconnected_session_fails_without_creating_retry(self):
        self.account.status = WhatsAppAccount.Status.PENDING
        self.account.save(update_fields=["status"])
        message = self._message()
        result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        self.assertEqual(result["reason"], "session_not_connected")
        message.refresh_from_db()
        self.assertEqual(message.status, "failed")
        self.gateway.send_message.assert_not_called()
        self.publish.assert_not_called()

    def test_api_account_cannot_cross_route_to_hosted_transport(self):
        self.account.connection_type = WhatsAppAccount.ConnectionType.API
        self.account.save(update_fields=["connection_type"])
        message = self._message()
        result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        self.assertEqual(result["reason"], "not_hosted")
        self.gateway.send_message.assert_not_called()

    def test_missing_provider_id_is_not_reported_as_sent(self):
        self.gateway.send_message.side_effect = None
        self.gateway.send_message.return_value = {}
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["message"]["status"], "failed")
        self.publish.assert_not_called()

    def test_pending_ack_schedules_same_row_only_after_first_attempt(self):
        self.gateway.send_message.side_effect = WhatsAppWebGatewayError(
            "Waiting for WhatsApp acknowledgement", status_code=503,
            response_body=json.dumps({"code": "provider_ack_pending"}),
        )
        response = self._post()
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertTrue(data["retry_scheduled"])
        self.assertEqual(data["message"]["status"], "queued")
        message = WhatsAppMessage.objects.get(pk=data["message"]["id"])
        self.assertEqual(message.raw_payload["shvya_hosted_request"]["request_id"], str(message.pk))
        self.assertEqual(self.publish.call_args.kwargs["args"], [str(message.pk)])
        self.assertEqual(self.publish.call_args.kwargs["retries"], 0)
        self.assertFalse(self.publish.call_args.kwargs["retry"])
        self.retry.assert_not_called()

    def test_hosted_retries_use_the_dedicated_hosted_worker_queue(self):
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[send_hosted_whatsapp_message_task.name]["queue"],
            "hosted_ai",
        )

    def test_ack_checks_continue_until_deadline_without_spending_send_retries(self):
        self.gateway.send_message.side_effect = WhatsAppWebGatewayError(
            "Waiting for acknowledgement", status_code=503,
            response_body=json.dumps({"code": "provider_ack_pending", "messageId": "pending-1"}),
        )
        message = self._message()
        send_hosted_whatsapp_message_task.push_request(retries=3)
        try:
            result = send_hosted_whatsapp_message_task.run(str(message.id))
        finally:
            send_hosted_whatsapp_message_task.pop_request()
        message.refresh_from_db()
        self.assertEqual(result["reason"], "provider_ack_pending")
        self.assertEqual(message.status, "queued")
        self.assertEqual(message.external_id, "wweb:pending-1")
        self.assertEqual(self.publish.call_args.kwargs["retries"], 3)
        self.assertGreaterEqual(self.publish.call_args.kwargs["countdown"], 15)
        self.assertLessEqual(self.publish.call_args.kwargs["countdown"], 21)
        self.retry.assert_not_called()

    def test_manual_ack_checks_stop_after_persisted_deadline(self):
        message = self._message()
        message.raw_payload["shvya_hosted_request"] = {
            "request_id": str(message.pk),
            "attempted_at": (timezone.now() - timedelta(seconds=301)).isoformat(),
        }
        message.save(update_fields=["raw_payload"])
        self.gateway.send_message.side_effect = WhatsAppWebGatewayError(
            "Waiting for acknowledgement", status_code=503,
            response_body=json.dumps({"code": "provider_ack_pending", "messageId": "pending-1"}),
        )
        result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        message.refresh_from_db()
        self.assertEqual(result["reason"], "provider_outcome_uncertain")
        self.assertEqual(message.status, "failed")
        self.assertTrue(self.gateway.send_message.call_args.kwargs["request_is_retry"])
        self.publish.assert_not_called()

    def test_provider_response_cannot_downgrade_callback_read_receipt(self):
        def callback_before_http_response(**kwargs):
            message = WhatsAppMessage.objects.get(pk=kwargs["request_id"])
            message.status = "read"
            message.external_id = "wweb:callback-read"
            message.raw_payload["lastAck"] = {"status": "read"}
            message.save(update_fields=["status", "external_id", "raw_payload"])
            return {"messageId": "callback-read", "status": "sent"}

        self.gateway.send_message.side_effect = callback_before_http_response
        response = self._post()
        message = WhatsAppMessage.objects.get(pk=response.json()["message"]["id"])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(message.status, "read")
        self.assertEqual(message.raw_payload["lastAck"], {"status": "read"})
        self.assertEqual(message.raw_payload["shvya_hosted"]["origin"], "agent")
        self.publish.assert_not_called()

    def test_malformed_http_result_cannot_erase_confirmed_callback_delivery(self):
        def callback_before_malformed_response(**kwargs):
            WhatsAppMessage.objects.filter(pk=kwargs["request_id"]).update(
                status="delivered", external_id="wweb:delivered-before-error",
            )
            return {}

        self.gateway.send_message.side_effect = callback_before_malformed_response
        response = self._post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["message"]["status"], "delivered")
        self.publish.assert_not_called()

    def test_realtime_publication_outage_does_not_fail_confirmed_delivery(self):
        self._patch(
            "services.channels.hosted_chat_service.queue_hosted_chat_refresh",
            side_effect=RuntimeError("Realtime broker unavailable"),
        )
        response = self._post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["message"]["status"], "sent")
        self.publish.assert_not_called()

    def test_ack_winning_task_requeue_does_not_schedule_another_provider_call(self):
        from services.channels.whatsapp_service import WhatsAppSendError

        message = self._message()

        def callback_then_retry(*, message):
            WhatsAppMessage.objects.filter(pk=message.pk).update(status="read")
            raise WhatsAppSendError("Gateway response lost") from WhatsAppWebGatewayError(
                "Gateway response lost", status_code=503,
            )

        with patch("services.channels.hosted_whatsapp_transport.send_hosted_message", side_effect=callback_then_retry):
            result = send_hosted_whatsapp_message_task.run(str(message.id), immediate=True)
        self.assertEqual(result["status"], "sent")
        message.refresh_from_db()
        self.assertEqual(message.status, "read")
        self.publish.assert_not_called()

    def test_provider_throttling_still_honours_retry_after(self):
        error = WhatsAppWebGatewayError("Rate limited", status_code=429)
        error.retry_after = 45
        self.gateway.send_message.side_effect = error
        response = self._post()
        self.assertEqual(response.status_code, 202)
        self.assertGreaterEqual(self.publish.call_args.kwargs["countdown"], 45)
        self.retry.assert_not_called()

    def test_uncertain_or_conflicting_outcome_is_not_automatically_resent(self):
        for code in ("provider_outcome_uncertain", "request_payload_conflict"):
            with self.subTest(code=code):
                self.gateway.send_message.side_effect = WhatsAppWebGatewayError(
                    "Cannot safely resend", status_code=409,
                    response_body=json.dumps({"code": code}),
                )
                response = self._post()
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["message"]["status"], "failed")
        self.publish.assert_not_called()

    def test_ambiguous_network_failure_does_not_claim_success(self):
        self.gateway.send_message.side_effect = WhatsAppWebGatewayError("Gateway lost response", status_code=502)
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["message"]["status"], "failed")
        self.publish.assert_not_called()

    def test_retry_broker_failure_is_visible_not_stuck_queued(self):
        self.gateway.send_message.side_effect = WhatsAppWebGatewayError("Session busy", status_code=503)
        self.publish.side_effect = RuntimeError("Broker unavailable")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["message"]["status"], "failed")
        self.assertIn("retry service", response.json()["error"])
        self.retry.assert_not_called()

    def test_account_from_another_organization_is_not_accessible(self):
        other = Organization.objects.create(name="Other Hosted Org")
        self.account.organization = other
        self.account.save(update_fields=["organization"])
        self.assertEqual(self._post().status_code, 404)
        self.gateway.send_message.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())

    def test_non_object_json_is_rejected(self):
        response = self.client.post(
            reverse("whatsapp-hosted-session-chat-send", args=[self.account.id]),
            data="[]", content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.gateway.send_message.assert_not_called()
