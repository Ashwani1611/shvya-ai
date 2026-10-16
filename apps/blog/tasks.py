from celery import shared_task
from django.db import OperationalError

from .services import publish_due_posts


@shared_task(name="apps.blog.tasks.publish_scheduled_posts", autoretry_for=(OperationalError,),
             retry_backoff=True, retry_kwargs={"max_retries": 3}, ignore_result=True)
def publish_scheduled_posts():
    """Minute-based durable sweep; row locks make overlapping/retried runs safe."""
    return publish_due_posts()
