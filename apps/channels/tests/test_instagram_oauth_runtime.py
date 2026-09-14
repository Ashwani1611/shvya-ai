from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase, override_settings

from services.channels.instagram_service import (
    build_authorize_url,
    instagram_app_id,
    instagram_app_secret,
    meta_credentials_available,
)


class InstagramOAuthRuntimeTests(SimpleTestCase):
    @override_settings(
        META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS=True,
        META_INSTAGRAM_APP_ID="",
        META_INSTAGRAM_APP_SECRET="",
        META_APP_ID="whatsapp-meta-app-id",
        META_APP_SECRET="whatsapp-meta-app-secret",
    )
    def test_production_does_not_fall_back_to_whatsapp_meta_credentials(self):
        self.assertEqual(instagram_app_id(), "")
        self.assertEqual(instagram_app_secret(), "")
        self.assertFalse(meta_credentials_available())

    @override_settings(
        META_INSTAGRAM_REQUIRE_DEDICATED_CREDENTIALS=True,
        META_INSTAGRAM_APP_ID="instagram-app-123",
        META_INSTAGRAM_APP_SECRET="instagram-secret-123",
        META_APP_ID="whatsapp-meta-app-id",
        META_APP_SECRET="whatsapp-meta-app-secret",
    )
    def test_production_prefers_dedicated_instagram_credentials(self):
        self.assertEqual(instagram_app_id(), "instagram-app-123")
        self.assertEqual(instagram_app_secret(), "instagram-secret-123")
        self.assertTrue(meta_credentials_available())

    def test_authorize_url_uses_current_instagram_business_login_parameters(self):
        url = build_authorize_url(
            app_id="instagram-app-123",
            redirect_uri=(
                "https://dashboard.shvya-ai.com/"
                "dashboard/instagram/connect/return/"
            ),
            state="signed-state",
        )
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.instagram.com")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["client_id"], ["instagram-app-123"])
        self.assertEqual(params["force_reauth"], ["true"])
        self.assertEqual(params["enable_fb_login"], ["0"])
        self.assertNotIn("force_authentication", params)
        self.assertIn("instagram_business_basic", params["scope"][0].split(","))
        self.assertIn(
            "instagram_business_manage_messages",
            params["scope"][0].split(","),
        )
