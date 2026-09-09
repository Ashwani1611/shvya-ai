from django.test import SimpleTestCase

from config.celery import app


class RealtimeAIQueueRoutingTests(SimpleTestCase):
    def test_whatsapp_engagement_and_single_send_use_realtime_queue(self):
        self.assertEqual(
            app.conf.task_routes["ai.generate_ai_engagement_response"]["queue"],
            "ai_realtime",
        )
        self.assertEqual(
            app.conf.task_routes[
                "apps.channels.tasks.send_whatsapp_message_task"
            ]["queue"],
            "ai_realtime",
        )

    def test_hosted_ai_wake_processing_and_recovery_use_dedicated_queue(self):
        self.assertEqual(
            app.conf.task_routes["hosted.dispatch_due_ai"]["queue"],
            "hosted_ai",
        )
        self.assertEqual(
            app.conf.task_routes[
                "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task"
            ]["queue"],
            "hosted_ai",
        )
        self.assertEqual(
            app.conf.beat_schedule[
                "dispatch-hosted-ai-recovery-every-10-seconds"
            ]["task"],
            "hosted.dispatch_due_ai",
        )
        self.assertEqual(
            app.conf.beat_schedule[
                "dispatch-hosted-ai-recovery-every-10-seconds"
            ]["schedule"],
            10.0,
        )
