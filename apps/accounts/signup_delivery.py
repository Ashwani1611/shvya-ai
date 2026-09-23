"""Deliver signup verification independently of account creation."""
import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.mail import get_connection, send_mail
from django.db import transaction
from django.utils import timezone

from .models import SignupVerificationDelivery

logger = logging.getLogger(__name__)


def deliver_signup_verification(delivery_id):
    # Serialize the immediate attempt and periodic recovery. SMTP has a bounded
    # timeout; failed deliveries stay in the database for a later attempt.
    with transaction.atomic():
        delivery = SignupVerificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if delivery is None or delivery.delivered_at or delivery.next_attempt_at > timezone.now():
            return
        user = delivery.user
        if user.is_active or user.email != delivery.email or not user.organization_id:
            delivery.delete()
            return
        token = signing.dumps(
            {"user_id": str(user.pk), "organization_id": str(user.organization_id), "email": user.email},
            salt="shvya-signup-email-v1", compress=True,
        )
        url = delivery.verification_endpoint + "?" + urlencode({"token": token})
        delivery.attempts += 1
        try:
            sent = send_mail(
                subject="Verify your SHVYA AI email",
                message=("Welcome to SHVYA AI.\n\n"
                         "Verify your email address to activate your organization:\n\n"
                         f"{url}\n\nThis verification link expires in 24 hours."),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[delivery.email],
                connection=get_connection(timeout=10),
                fail_silently=False,
            )
            if sent != 1:
                raise RuntimeError("Verification message was not accepted")
        except Exception as exc:
            # Provider exception text can contain credentials or personal data.
            delivery.error_type = type(exc).__name__[:100]
            delivery.next_attempt_at = timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** min(delivery.attempts - 1, 6)))
            logger.warning("Signup verification delivery failed: delivery_id=%s error_type=%s", delivery.pk, delivery.error_type)
        else:
            delivery.delivered_at = timezone.now()
            delivery.error_type = ""
        delivery.save(update_fields=["attempts", "next_attempt_at", "delivered_at", "error_type"])
