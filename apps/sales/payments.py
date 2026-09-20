from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

import requests
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.sales.activity import record_activity
from apps.sales.lifecycle import invoice_ledger, record_payment, record_refund
from apps.sales.models import DocumentType
from apps.sales.models_lifecycle import (
    SalesPaymentCheckout,
    SalesPaymentGateway,
)


class SalesGatewayError(RuntimeError):
    pass


def _minor_units(amount):
    return int(
        (Decimal(str(amount)) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _absolute(base_url, path):
    return urljoin(str(base_url or "").rstrip("/") + "/", path.lstrip("/"))


def create_payment_checkout(*, invoice, gateway, actor=None, base_url):
    if invoice.document_type != DocumentType.INVOICE:
        raise ValidationError("Payment links can only be created for invoices.")
    if invoice.status in {"draft", "cancelled"}:
        raise ValidationError("Send the invoice before creating a payment link.")
    if gateway.organization_id != invoice.organization_id or not gateway.is_enabled:
        raise ValidationError("Payment gateway is not enabled for this organization.")

    ledger = invoice_ledger(invoice)
    amount = ledger["balance"]
    if amount <= 0:
        raise ValidationError("This invoice has no outstanding balance.")

    checkout = SalesPaymentCheckout.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        gateway=gateway,
        amount=amount,
        currency=invoice.currency.upper(),
        created_by=actor,
    )
    secret = gateway.get_secret()
    if not secret:
        checkout.status = SalesPaymentCheckout.Status.FAILED
        checkout.save(update_fields=["status"])
        raise SalesGatewayError("Payment gateway secret is missing.")

    try:
        if gateway.provider == SalesPaymentGateway.Provider.RAZORPAY:
            if not gateway.public_key:
                raise SalesGatewayError("Razorpay Key ID is missing.")
            response = requests.post(
                "https://api.razorpay.com/v1/payment_links",
                auth=(gateway.public_key, secret),
                json={
                    "amount": _minor_units(amount),
                    "currency": invoice.currency.upper(),
                    "accept_partial": False,
                    "description": f"Invoice {invoice.document_number}",
                    "reference_id": str(checkout.id),
                    "customer": {
                        "name": invoice.recipient_name or "",
                        "email": invoice.recipient_email or "",
                        "contact": invoice.recipient_phone or "",
                    },
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": False,
                },
                timeout=20,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                raise SalesGatewayError(
                    str(data.get("error", {}).get("description") or "Razorpay rejected the payment link.")
                )
            checkout.provider_reference = str(data.get("id") or "")
            checkout.checkout_url = str(data.get("short_url") or "")
        elif gateway.provider == SalesPaymentGateway.Provider.STRIPE:
            success_url = _absolute(
                base_url,
                reverse("shvya-sales-public-document", args=[invoice.public_token]),
            )
            response = requests.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers={"Authorization": f"Bearer {secret}"},
                data={
                    "mode": "payment",
                    "success_url": success_url + "?payment=success",
                    "cancel_url": success_url + "?payment=cancelled",
                    "client_reference_id": str(checkout.id),
                    "customer_email": invoice.recipient_email or "",
                    "line_items[0][quantity]": "1",
                    "line_items[0][price_data][currency]": invoice.currency.lower(),
                    "line_items[0][price_data][unit_amount]": str(_minor_units(amount)),
                    "line_items[0][price_data][product_data][name]": (
                        f"Invoice {invoice.document_number}"
                    ),
                    "metadata[checkout_id]": str(checkout.id),
                    "metadata[invoice_id]": str(invoice.id),
                },
                timeout=20,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                message = data.get("error", {}).get("message") if isinstance(data, dict) else ""
                raise SalesGatewayError(message or "Stripe rejected the checkout request.")
            checkout.provider_reference = str(data.get("id") or "")
            checkout.checkout_url = str(data.get("url") or "")
        else:
            raise SalesGatewayError("Unsupported payment gateway.")
    except (requests.RequestException, ValueError) as exc:
        checkout.status = SalesPaymentCheckout.Status.FAILED
        checkout.save(update_fields=["status"])
        raise SalesGatewayError("Payment gateway could not be reached safely.") from exc
    except SalesGatewayError:
        checkout.status = SalesPaymentCheckout.Status.FAILED
        checkout.save(update_fields=["status"])
        raise

    if not checkout.provider_reference or not checkout.checkout_url:
        checkout.status = SalesPaymentCheckout.Status.FAILED
        checkout.save(update_fields=["status"])
        raise SalesGatewayError("Payment gateway returned an incomplete checkout.")

    checkout.save(
        update_fields=[
            "provider_reference",
            "checkout_url",
            "status",
        ]
    )
    record_activity(
        invoice,
        event_type="payment_link_created",
        message=f"{gateway.get_provider_display()} payment link created.",
        actor=actor,
        metadata={
            "checkout_id": str(checkout.id),
            "provider": gateway.provider,
            "amount": str(amount),
        },
    )
    return checkout


def verify_razorpay_webhook(*, gateway, body, signature):
    secret = gateway.get_webhook_secret()
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def verify_stripe_webhook(*, gateway, body, signature_header, tolerance=300):
    secret = gateway.get_webhook_secret()
    if not secret or not signature_header:
        return False

    timestamp = None
    signatures = []
    for part in str(signature_header).split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or abs(int(time.time()) - timestamp) > tolerance:
        return False
    signed = f"{timestamp}.".encode("utf-8") + body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, value) for value in signatures)


def _checkout_from_event(*, gateway, provider_reference="", checkout_id=""):
    queryset = SalesPaymentCheckout.objects.select_related("invoice").filter(
        gateway=gateway,
        organization=gateway.organization,
    )
    if checkout_id:
        try:
            return queryset.get(pk=checkout_id)
        except (SalesPaymentCheckout.DoesNotExist, ValueError):
            pass
    if provider_reference:
        return queryset.filter(provider_reference=provider_reference).first()
    return None


@transaction.atomic
def apply_razorpay_event(*, gateway, body):
    payload = json.loads(body.decode("utf-8"))
    event = str(payload.get("event") or "")
    if event not in {"payment_link.paid", "payment.captured"}:
        return None

    payment_link = (
        payload.get("payload", {})
        .get("payment_link", {})
        .get("entity", {})
    )
    payment = (
        payload.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )
    provider_reference = str(payment_link.get("id") or "")
    checkout_id = str(payment_link.get("reference_id") or "")
    checkout = _checkout_from_event(
        gateway=gateway,
        provider_reference=provider_reference,
        checkout_id=checkout_id,
    )
    if checkout is None:
        return None

    external_payment_id = str(payment.get("id") or "")
    if not external_payment_id:
        return None
    amount = Decimal(str(payment.get("amount") or 0)) / Decimal("100")
    if amount <= 0:
        return None

    if checkout.payments.filter(
        provider=gateway.provider,
        external_payment_id=external_payment_id,
        kind="payment",
    ).exists():
        return checkout

    record_payment(
        invoice=checkout.invoice,
        amount=amount,
        method="gateway",
        provider=gateway.provider,
        external_payment_id=external_payment_id,
        reference_number=provider_reference,
        checkout=checkout,
    )
    checkout.status = SalesPaymentCheckout.Status.PAID
    checkout.external_payment_id = external_payment_id
    checkout.paid_at = timezone.now()
    checkout.save(
        update_fields=[
            "status",
            "external_payment_id",
            "paid_at",
        ]
    )
    return checkout


@transaction.atomic
def apply_stripe_event(*, gateway, body):
    payload = json.loads(body.decode("utf-8"))
    if str(payload.get("type") or "") != "checkout.session.completed":
        return None
    session = payload.get("data", {}).get("object", {})
    provider_reference = str(session.get("id") or "")
    metadata = session.get("metadata") or {}
    checkout = _checkout_from_event(
        gateway=gateway,
        provider_reference=provider_reference,
        checkout_id=str(metadata.get("checkout_id") or session.get("client_reference_id") or ""),
    )
    if checkout is None:
        return None

    payment_intent = str(session.get("payment_intent") or provider_reference)
    if checkout.payments.filter(
        provider=gateway.provider,
        external_payment_id=payment_intent,
        kind="payment",
    ).exists():
        return checkout

    amount = Decimal(str(session.get("amount_total") or 0)) / Decimal("100")
    if amount <= 0:
        return None
    record_payment(
        invoice=checkout.invoice,
        amount=amount,
        method="gateway",
        provider=gateway.provider,
        external_payment_id=payment_intent,
        reference_number=provider_reference,
        checkout=checkout,
    )
    checkout.status = SalesPaymentCheckout.Status.PAID
    checkout.external_payment_id = payment_intent
    checkout.paid_at = timezone.now()
    checkout.save(
        update_fields=[
            "status",
            "external_payment_id",
            "paid_at",
        ]
    )
    return checkout


def refund_gateway_payment(*, payment, amount, actor=None, note=""):
    if payment.kind != "payment" or payment.status != "succeeded":
        raise ValidationError("Only successful payments can be refunded.")
    if not payment.provider or not payment.external_payment_id:
        raise ValidationError("This payment is not linked to a refundable gateway transaction.")

    gateway = SalesPaymentGateway.objects.filter(
        organization=payment.organization,
        provider=payment.provider,
        is_enabled=True,
    ).first()
    if gateway is None:
        raise ValidationError("The original payment gateway is not enabled.")
    secret = gateway.get_secret()
    if not secret:
        raise SalesGatewayError("Payment gateway secret is missing.")

    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Refund amount must be greater than zero.")

    previous_refunds = payment.invoice.payments.filter(
        kind="refund",
        status="succeeded",
        provider=payment.provider,
        reference_number=payment.external_payment_id,
    )
    already_refunded = sum(
        (item.amount for item in previous_refunds),
        start=Decimal("0"),
    )
    if amount > payment.amount - already_refunded:
        raise ValidationError("Refund exceeds the remaining amount of this payment.")

    try:
        if gateway.provider == SalesPaymentGateway.Provider.RAZORPAY:
            response = requests.post(
                f"https://api.razorpay.com/v1/payments/{payment.external_payment_id}/refund",
                auth=(gateway.public_key, secret),
                json={"amount": _minor_units(amount)},
                timeout=20,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                raise SalesGatewayError(
                    str(data.get("error", {}).get("description") or "Razorpay refund failed.")
                )
            refund_id = str(data.get("id") or "")
        elif gateway.provider == SalesPaymentGateway.Provider.STRIPE:
            response = requests.post(
                "https://api.stripe.com/v1/refunds",
                headers={"Authorization": f"Bearer {secret}"},
                data={
                    "payment_intent": payment.external_payment_id,
                    "amount": str(_minor_units(amount)),
                },
                timeout=20,
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                raise SalesGatewayError(
                    str(data.get("error", {}).get("message") or "Stripe refund failed.")
                )
            refund_id = str(data.get("id") or "")
        else:
            raise SalesGatewayError("Unsupported payment gateway.")
    except requests.RequestException as exc:
        raise SalesGatewayError("Payment gateway could not be reached safely.") from exc

    if not refund_id:
        raise SalesGatewayError("Gateway returned no refund identifier.")
    return record_refund(
        invoice=payment.invoice,
        amount=amount,
        actor=actor,
        provider=gateway.provider,
        external_payment_id=refund_id,
        reference_number=payment.external_payment_id,
        note=note,
    )
