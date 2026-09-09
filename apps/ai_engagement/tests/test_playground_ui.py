from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.ai_engagement.views.playground import (
    CRMPlaygroundSessionAuthentication,
    PlaygroundAPIView,
)


class PlaygroundUIRegressionTests(SimpleTestCase):
    def test_playground_api_route_is_wired(self):
        match = resolve("/api/v1/ai-engagement/playground/")

        self.assertIs(
            match.func.view_class,
            PlaygroundAPIView,
        )

    def test_playground_accepts_crm_session_and_jwt_authentication(self):
        self.assertEqual(
            PlaygroundAPIView.authentication_classes,
            [
                CRMPlaygroundSessionAuthentication,
                JWTAuthentication,
            ],
        )

    @patch(
        "apps.ai_engagement.views.playground."
        "SessionAuthentication.enforce_csrf"
    )
    @patch(
        "apps.ai_engagement.views.playground."
        "get_crm_authenticated_user"
    )
    def test_crm_session_authentication_accepts_active_dashboard_user(
        self,
        get_crm_authenticated_user,
        enforce_csrf,
    ):
        user = SimpleNamespace(
            is_active=True,
            is_authenticated=True,
        )
        get_crm_authenticated_user.return_value = user

        django_request = APIRequestFactory().post(
            "/api/v1/ai-engagement/playground/",
            {},
            format="json",
        )
        request = Request(django_request)

        result = CRMPlaygroundSessionAuthentication().authenticate(
            request
        )

        self.assertEqual(
            result,
            (user, None),
        )
        get_crm_authenticated_user.assert_called_once_with(
            django_request
        )
        enforce_csrf.assert_called_once_with(request)

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

    def test_playground_client_never_renders_boolean_error_flag_as_text(self):
        script = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "ai_setup_playground.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "const detailText = firstErrorText(payload.detail);",
            script,
        )
        self.assertIn(
            'typeof payload.error === "string"',
            script,
        )
        self.assertNotIn(
            "payload.error ||\n                payload.detail",
            script,
        )

    def test_playground_client_only_renders_string_ai_responses(self):
        script = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "ai_setup_playground.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'typeof payload.response === "string"',
            script,
        )
