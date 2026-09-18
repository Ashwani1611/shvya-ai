"""Pure projection/policy regressions; no Django or provider access required."""
from datetime import datetime, timedelta, timezone
import unittest

from services.channels.instagram_content import display_attachments, reply_window, safe_error, safe_url


class InstagramContentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)

    def test_standard_window_open(self):
        self.assertTrue(reply_window(self.now - timedelta(hours=23), self.now)["can_reply"])

    def test_exact_24_hour_boundary_is_closed(self):
        self.assertFalse(reply_window(self.now - timedelta(hours=24), self.now)["can_reply"])

    def test_missing_inbound_is_closed(self):
        self.assertFalse(reply_window(None, self.now)["can_reply"])

    def test_future_or_naive_timestamp_does_not_grant_window(self):
        for value in [self.now + timedelta(seconds=1), self.now.replace(tzinfo=None)]:
            with self.subTest(value=value):
                self.assertFalse(reply_window(value, self.now)["can_reply"])

    def test_human_agent_never_implicitly_enabled(self):
        self.assertFalse(reply_window(self.now, self.now)["human_agent_enabled"])

    def test_https_media_url_is_preserved(self):
        url = "https://scontent.cdninstagram.com/media?oe=123&oh=signature"
        self.assertEqual(safe_url(url), url)

    def test_unsafe_urls_never_reach_browser(self):
        for url in ["javascript:alert(1)", "data:text/html,<script>", "//evil.test/a", "http://example.com/a", "https://x.test:8080/a", "https://user:pass@x.test/a", "https://x.test/\nsecret", "https://x.test\\@evil.test", "https://x.test/?access_token=secret", "https://x.test/?client_secret=secret"]:
            with self.subTest(url=url):
                self.assertEqual(safe_url(url), "")

    def test_webhook_images_audio_and_video(self):
        for kind in ["image", "audio", "video"]:
            with self.subTest(kind=kind):
                media = display_attachments([{"type": kind, "payload": {"url": "https://cdn.example.com/media"}}])
                self.assertEqual(media[0]["kind"], kind)
                self.assertFalse(media[0]["unavailable"])

    def test_graph_attachment_shape(self):
        media = display_attachments({"data": [{"video_data": {"url": "https://cdn.example.com/v", "preview_url": "https://cdn.example.com/p"}}]})
        self.assertEqual(media[0]["kind"], "video")
        self.assertEqual(media[0]["preview_url"], "https://cdn.example.com/p")

    def test_story_reply_context_is_kept_alongside_attachment(self):
        media = display_attachments([{"type":"image", "payload":{"url":"https://cdn.example.com/i"}}], {"message":{"text":"Price?", "reply_to":{"story":{"id":"story", "url":"https://www.instagram.com/stories/business/123"}}}})
        self.assertEqual([item["label"] for item in media], ["Reply to story", "Image"])

    def test_reel_permalink_is_not_invented_as_video(self):
        media = display_attachments([{"type":"ig_reel", "payload":{"url":"https://www.instagram.com/reel/example"}}])
        self.assertEqual(media[0]["kind"], "link")
        self.assertEqual(media[0]["label"], "Instagram reel")

    def test_deleted_message_does_not_expose_old_media(self):
        self.assertEqual(display_attachments([{"type":"image", "payload":{"url":"https://cdn.example.com/i"}}], {"message":{"is_deleted":True}}), [])

    def test_unsupported_content_has_explicit_fallback(self):
        self.assertTrue(display_attachments([], {"message":{"is_unsupported":True}})[0]["unavailable"])

    def test_malformed_attachment_entries_do_not_crash(self):
        self.assertEqual(display_attachments([None, "bad", 2]), [])
        self.assertTrue(display_attachments([{"type":"video", "payload": []}])[0]["unavailable"])

    def test_secret_values_and_token_query_parameters_are_redacted(self):
        text = safe_error("https://graph.instagram.com/access_token?access_token=topsecret&client_secret=appsecret Bearer bearer-secret", ("topsecret", "appsecret"))
        self.assertNotIn("topsecret", text)
        self.assertNotIn("appsecret", text)
        self.assertNotIn("bearer-secret", text)


if __name__ == "__main__":
    unittest.main()
