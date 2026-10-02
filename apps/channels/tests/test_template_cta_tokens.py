from types import SimpleNamespace

from django.core import signing
from django.test import SimpleTestCase

from services.channels.template_cta_tracking import (
    decode_cta_token,
    encode_cta_token,
)


class TemplateCTATokenTests(SimpleTestCase):
    def test_round_trip_preserves_button_action(self):
        token = encode_cta_token(
            template=SimpleNamespace(pk="00000000-0000-0000-0000-000000000001"),
            action_type="url",
            destination="https://example.test/offers",
            label="View offer",
            button_index=1,
            card_index=2,
        )

        payload = decode_cta_token(token)

        self.assertEqual(payload["a"], "url")
        self.assertEqual(payload["d"], "https://example.test/offers")
        self.assertEqual(payload["l"], "View offer")
        self.assertEqual(payload["b"], 1)
        self.assertEqual(payload["c"], 2)

    def test_tampered_token_is_rejected(self):
        token = encode_cta_token(
            template=SimpleNamespace(pk="00000000-0000-0000-0000-000000000001"),
            action_type="call",
            destination="+15555550100",
            label="Call",
            button_index=0,
        )

        with self.assertRaises(signing.BadSignature):
            decode_cta_token(token + "tampered")
