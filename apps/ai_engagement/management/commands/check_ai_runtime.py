from django.core.management.base import BaseCommand, CommandError
from config.celery import app

REQUIRED = {
    'ai_realtime': {'ai.generate_ai_engagement_response', 'apps.channels.tasks.send_whatsapp_message_task'},
    'hosted_ai': {'hosted.dispatch_due_ai', 'apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task'},
}


def missing_consumers(queues, registered):
    missing = []
    for queue, tasks in REQUIRED.items():
        if not any(
            queue in {item.get('name') for item in worker_queues}
            and tasks <= set(registered.get(worker, []))
            for worker, worker_queues in queues.items()
        ):
            missing.append(queue)
    return missing


class Command(BaseCommand):
    help = 'Verify live AI workers consume their queues and register inbound tasks.'

    def handle(self, *args, **options):
        inspect = app.control.inspect(timeout=5)
        missing = missing_consumers(inspect.active_queues() or {}, inspect.registered() or {})
        if missing:
            raise CommandError('No ready AI consumer for: ' + ', '.join(missing))
        self.stdout.write(self.style.SUCCESS('Hosted and WhatsApp API AI consumers are ready.'))
