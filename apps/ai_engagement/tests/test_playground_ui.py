from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve

from apps.ai_engagement.views.playground import PlaygroundAPIView


class PlaygroundUIRegressionTests(SimpleTestCase):
    def test_playground_api_route_is_wired(self):
        match = resolve("/api/v1/ai-engagement/playground/")

        self.assertIs(
            match.func.view_class,
            PlaygroundAPIView,
        )

    def test_ai_setup_loads_playground_client(self):
        template = (
            Path(settings.BASE_DIR)
            / "templates"
            / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "js/ai_setup_playground.js",
            template,
        )

    def test_playground_client_posts_expected_payload_with_csrf(self):
        script = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "ai_setup_playground.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'const PLAYGROUND_ENDPOINT = "/api/v1/ai-engagement/playground/";',
            script,
        )
        self.assertIn(
            "fetch(PLAYGROUND_ENDPOINT",
            script,
        )
        self.assertIn(
            '"X-CSRFToken": csrfToken',
            script,
        )
        self.assertIn(
            "session_id: sessionId",
            script,
        )
        self.assertIn(
            "message: messageText",
            script,
        )
        self.assertIn(
            "history: history.slice(-MAX_HISTORY_MESSAGES)",
            script,
        )
