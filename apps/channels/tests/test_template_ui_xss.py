from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.channels import template_ui


class TemplateEditorInlineJsonSecurityTests(SimpleTestCase):
    def _decode_expression(self, expression: str):
        prefix = 'JSON.parse(atob("'
        suffix = '"))'
        self.assertTrue(expression.startswith(prefix))
        self.assertTrue(expression.endswith(suffix))
        encoded = expression[len(prefix) : -len(suffix)]
        payload = base64.b64decode(encoded).decode("ascii")
        return json.loads(payload)

    def test_inline_json_blocks_script_breakout_and_round_trips(self):
        placeholders = [
            {
                "key": "custom_field",
                "label": '</script><script>alert("stored-xss")</script>',
                "description": "A&B <dangerous> ' \" value",
                "example": "line\u2028separator\u2029test",
            }
        ]

        expression = template_ui._inline_script_json(placeholders)

        self.assertNotIn("</script", expression.lower())
        self.assertNotIn("<", expression)
        self.assertNotIn(">", expression)
        self.assertNotIn("&", expression)
        self.assertNotIn("stored-xss", expression)
        self.assertEqual(self._decode_expression(expression), placeholders)

    def test_inline_json_preserves_unicode_data(self):
        placeholders = [
            {
                "key": "city",
                "label": "शहर",
                "description": "दिल्ली / 東京 / München",
                "example": "नई दिल्ली",
            }
        ]

        expression = template_ui._inline_script_json(placeholders)

        self.assertTrue(expression.isascii())
        self.assertEqual(self._decode_expression(expression), placeholders)

    @patch("apps.channels.template_ui.render")
    @patch("apps.channels.template_ui._accounts", return_value=[])
    @patch("apps.channels.template_ui.available_placeholders")
    def test_render_editor_never_passes_raw_placeholder_json_to_template(
        self,
        available_placeholders,
        _accounts,
        render,
    ):
        placeholders = [
            {
                "key": "danger",
                "label": "</script><script>window.pwned=true</script>",
                "description": "tenant supplied",
                "example": "example",
            }
        ]
        available_placeholders.return_value = placeholders
        render.return_value = object()
        user = SimpleNamespace(
            organization=SimpleNamespace(pk=1),
        )

        response = template_ui._render_editor(
            request=object(),
            user=user,
            values={
                "buttons_json": "[]",
                "carousel_json": "{}",
            },
            template=None,
        )

        self.assertIs(response, render.return_value)
        context = render.call_args.args[2]
        expression = context["placeholders_json"]

        self.assertNotIn("</script", expression.lower())
        self.assertNotIn("window.pwned", expression)
        self.assertEqual(self._decode_expression(expression), placeholders)
