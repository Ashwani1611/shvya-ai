from celery import shared_task
from django.utils import timezone

from .models import SignupVerificationDelivery
from .signup_delivery import deliver_signup_verification


@shared_task(name="accounts.deliver_signup_verifications")
def deliver_signup_verifications():
    due_ids = list(SignupVerificationDelivery.objects.filter(
        delivered_at__isnull=True, next_attempt_at__lte=timezone.now(),
    ).order_by("next_attempt_at").values_list("pk", flat=True)[:50])
    for delivery_id in due_ids:
        deliver_signup_verification(delivery_id)
