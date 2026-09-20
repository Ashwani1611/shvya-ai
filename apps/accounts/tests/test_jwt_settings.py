from django.conf import settings
from django.test import SimpleTestCase


class JWTSettingsTests(SimpleTestCase):
    def test_simplejwt_signing_key_uses_configured_jwt_secret(self):
        self.assertTrue(settings.JWT_SECRET)
        self.assertEqual(
            settings.SIMPLE_JWT["SIGNING_KEY"],
            settings.JWT_SECRET,
        )
