from __future__ import annotations

from threading import Barrier, Lock, Thread
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
from django.test import TransactionTestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message


class WhatsAppInboundIdempotencyTests(TransactionTestCase):
    """Meta can deliver the same wamid concurrently; persist it only once."""

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Inbound Idempotency Test Org",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Inbound Idempotency WhatsApp",
            phone_number_id="phone-idempotency-1",
            display_phone_number="+15551234567",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _inbound(self, *, external_id: str, body: str):
        return handle_inbound_message(
            organization=self.organization,
            account=self.account,
            external_id=external_id,
            from_number="15559876543",
            to_number=self.account.phone_number_id,
            body=body,
            raw_payload={"id": external_id, "body": body},
        )

    @patch("services.channels.whatsapp_service.resolve_pipeline", return_value=None)
    @patch("services.channels.realtime.queue_message_publish")
    def test_sequential_duplicate_reuses_canonical_row_without_overwrite(
        self,
        queue_message_publish,
        _resolve_pipeline,
    ):
        first = self._inbound(
            external_id="wamid-idempotency-sequential",
            body="Original delivery",
        )
        duplicate = self._inbound(
            external_id="wamid-idempotency-sequential",
            body="Duplicate retry must not overwrite",
        )

        first.refresh_from_db()
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(first.body, "Original delivery")
        # Runtime layers are allowed to append SHVYA diagnostics to raw_payload;
        # a duplicate Meta delivery must not replace the canonical provider data.
        self.assertEqual(
            first.raw_payload["id"],
            "wamid-idempotency-sequential",
        )
        self.assertEqual(
            first.raw_payload["body"],
            "Original delivery",
        )
        self.assertEqual(
            WhatsAppMessage.objects.filter(
                external_id="wamid-idempotency-sequential"
            ).count(),
            1,
        )
        queue_message_publish.assert_called_once()

    def test_concurrent_duplicate_wamid_returns_same_row_without_integrity_error(self):
        if connection.vendor != "postgresql":
            self.skipTest("The production idempotency race is PostgreSQL-specific.")

        external_id = "wamid-idempotency-concurrent"
        gate = Barrier(2, timeout=10)
        result_lock = Lock()
        message_ids: list[str] = []
        errors: list[BaseException] = []
        original_get = QuerySet.get

        def synchronize_missing_message_lookup(queryset, *args, **kwargs):
            try:
                return original_get(queryset, *args, **kwargs)
            except WhatsAppMessage.DoesNotExist:
                if (
                    queryset.model is WhatsAppMessage
                    and kwargs.get("external_id") == external_id
                ):
                    # Force both get_or_create calls to observe the row as
                    # absent before either transaction attempts its INSERT.
                    # The unique external_id constraint must then decide the
                    # winner and the losing caller must reuse that committed row.
                    gate.wait()
                raise

        def worker(body: str):
            close_old_connections()
            try:
                organization = Organization.objects.get(
                    pk=self.organization.pk,
                )
                account = WhatsAppAccount.objects.get(
                    pk=self.account.pk,
                )
                message = handle_inbound_message(
                    organization=organization,
                    account=account,
                    external_id=external_id,
                    from_number="15559876543",
                    to_number=account.phone_number_id,
                    body=body,
                    raw_payload={"id": external_id, "body": body},
                )
                with result_lock:
                    message_ids.append(str(message.pk))
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        with (
            patch(
                "services.channels.whatsapp_service.resolve_pipeline",
                return_value=None,
            ),
            patch.object(
                QuerySet,
                "get",
                new=synchronize_missing_message_lookup,
            ),
            patch("services.channels.realtime.queue_message_publish") as publish,
        ):
            first = Thread(target=worker, args=("Concurrent delivery A",))
            second = Thread(target=worker, args=("Concurrent delivery B",))
            first.start()
            second.start()
            first.join(timeout=15)
            second.join(timeout=15)

            self.assertFalse(first.is_alive(), "first webhook worker did not finish")
            self.assertFalse(second.is_alive(), "second webhook worker did not finish")

        self.assertEqual(errors, [])
        self.assertEqual(len(message_ids), 2)
        self.assertEqual(len(set(message_ids)), 1)
        self.assertEqual(
            WhatsAppMessage.objects.filter(external_id=external_id).count(),
            1,
        )
        self.assertEqual(publish.call_count, 1)

        stored = WhatsAppMessage.objects.get(external_id=external_id)
        self.assertEqual(stored.direction, WhatsAppMessage.Direction.INBOUND)
        self.assertEqual(stored.status, WhatsAppMessage.Status.RECEIVED)
