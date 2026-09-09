from django.test import SimpleTestCase

from config.celery import app


class RealtimeAIQueueRoutingTests(SimpleTestCase):
    def test_whatsapp_engagement_uses_dedicated_realtime_queue(self):
        self.assertEqual(
            app.conf.task_routes["ai.generate_ai_engagement_response"]["queue"],
            "ai_realtime",
        )

    def test_hosted_ai_wake_and_processing_use_dedicated_queue(self):
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
