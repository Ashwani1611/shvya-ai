from celery import shared_task


@shared_task(name="support.deliver_notifications", ignore_result=True, soft_time_limit=150, time_limit=180)
def deliver_notifications():
    from .jobs import deliver_pending
    return deliver_pending(limit=5)


@shared_task(name="support.maintain_tickets", ignore_result=True, soft_time_limit=110, time_limit=120)
def maintain_tickets():
    from .jobs import maintain_tickets as run
    return run()


@shared_task(name="support.poll_mailbox", ignore_result=True, soft_time_limit=150, time_limit=180)
def poll_mailbox():
    from .mail import poll_mailbox as run
    return run(limit=5)
