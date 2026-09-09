from django.test import SimpleTestCase

from config.celery import app


class RealtimeAIQueueRoutingTests(SimpleTestCase):
    def test_whatsapp_engagement_uses_dedicated_realtime_queue(self):
        self.assertEqual(
            app.conf.task_routes["ai.generate_ai_engagement_response"]["queue"],
            "ai_realtime",
        )
