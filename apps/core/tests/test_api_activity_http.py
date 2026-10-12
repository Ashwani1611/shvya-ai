"""Realistic transport responses verify observation without changing delivery."""
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
import requests

from apps.core.api_activity.http import observed_request
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.channels.providers.whatsapp_web import WhatsAppWebClient, WhatsAppWebGatewayError


class APIActivityHTTPTests(SimpleTestCase):
    def setUp(self):
        self.activity = object()
        self.start = patch("apps.core.api_activity.http.start_activity", return_value=self.activity).start()
        self.finish = patch("apps.core.api_activity.http.finish_activity").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(status, payload):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    def test_provider_error_records_codes_without_secret_payload_or_url(self):
        response = self.response(400, {"error": {"code": 131042, "error_subcode": 99,
                "type": "OAuthException", "fbtrace_id": "trace-id", "message": "token=secret-value buyer@example.com"}})
        call = Mock(return_value=response)
        result = observed_request("POST", "https://graph.facebook.com/secret-path?access_token=secret-value",
                request_callable=call, integration="whatsapp_api", operation="send",
                headers={"Authorization": "Bearer secret-value"}, json={"body": "customer-private-message"})
        self.assertIs(result, response)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.start.call_args.kwargs["endpoint"], "graph.facebook.com")
        observed = self.finish.call_args.kwargs
        self.assertEqual(observed["status"], "failed")
        self.assertEqual(observed["error_code"], "131042")
        self.assertEqual(observed["metadata"]["provider_error_subcode"], "99")
        captured = repr((self.start.call_args, self.finish.call_args))
        for secret in ("secret-value", "buyer@example.com", "customer-private-message", "secret-path"):
            self.assertNotIn(secret, captured)

    def test_post_timeout_is_unknown_and_same_exception_is_raised(self):
        error = requests.Timeout("https://host/path?token=secret")
        call = Mock(side_effect=error)
        with self.assertRaises(requests.Timeout) as caught:
            observed_request("POST", "https://host", request_callable=call,
                             integration="whatsapp_api", operation="send")
        self.assertIs(caught.exception, error)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.finish.call_args.kwargs["status"], "unknown")
        self.assertNotIn("token=secret", repr(self.finish.call_args))

    def test_get_timeout_is_failure_and_stream_response_is_not_consumed(self):
        with self.assertRaises(requests.Timeout):
            observed_request("GET", "https://host", request_callable=Mock(side_effect=requests.Timeout()),
                             integration="instagram", operation="fetch")
        self.assertEqual(self.finish.call_args.kwargs["status"], "failed")
        response = Mock(status_code=503)
        observed_request("GET", "https://host", request_callable=Mock(return_value=response),
                         integration="whatsapp_api", operation="download", stream=True)
        response.json.assert_not_called()

    @patch("apps.channels.providers.whatsapp.requests.post")
    def test_whatsapp_real_adapter_preserves_provider_error_and_observes_once(self, post):
        post.return_value = self.response(429, {"error": {"code": 130429, "message": "rate limited"}})
        with self.assertRaises(WhatsAppAPIError) as caught:
            WhatsAppClient("123456", "private-access-token").send_text_message(to="919876543210", body="private body")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(self.finish.call_args.kwargs["error_code"], "130429")
        self.assertEqual(post.call_count, 1)

    @patch("apps.channels.providers.whatsapp.requests.post")
    def test_whatsapp_acceptance_is_not_delivery(self, post):
        post.return_value = self.response(200, {"messages": [{"id": "wamid.123"}]})
        result = WhatsAppClient("123456", "private-token").send_text_message(to="919876543210", body="private body")
        self.assertEqual(result["messages"][0]["id"], "wamid.123")
        self.assertEqual(self.finish.call_args.kwargs["status"], "accepted")
        self.assertNotIn("private body", repr(self.finish.call_args))

    @patch("apps.channels.providers.whatsapp_web.requests.request")
    def test_hosted_boundary_preserves_timeout(self, request):
        request.side_effect = requests.Timeout("private gateway diagnostic")
        client = object.__new__(WhatsAppWebClient)
        client.base_url, client.token, client.shard = "http://gateway:3000", "private-token", "primary"
        with self.assertRaises(WhatsAppWebGatewayError):
            client._request("POST", "/sessions/private-session/messages", payload={"body": "private body"})
        self.assertEqual(self.start.call_args.kwargs["integration"], "whatsapp_hosted")
        self.assertEqual(self.finish.call_args.kwargs["status"], "unknown")
