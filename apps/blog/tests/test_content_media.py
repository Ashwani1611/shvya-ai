from io import BytesIO
from uuid import uuid4

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from apps.blog import services
from apps.blog.content import sanitize_html
from apps.blog.models import BlogImage
from apps.blog.storage import MAGIC
from apps.blog.tests.helpers import BlogTestCase, image_upload


class RichTextSanitizationTests(SimpleTestCase):
    def test_sanitizer_keeps_standard_format_and_removes_active_content(self):
        source = (
            '<h2 onclick="alert(1)">Useful heading</h2>'
            '<p style="background:url(javascript:alert(1))">A <strong>clear</strong> guide.</p>'
            '<script>alert("secret-script")</script><style>secret-style{}</style>'
            '<iframe src="https://example.com">secret-iframe</iframe>'
            '<svg onload="alert(1)"><script>alert(2)</script></svg>'
            '<table><tbody><tr><td colspan="2" onmouseover="alert(1)">Cell</td></tr></tbody></table>'
            '<a href="javascript:alert(1)">Unsafe link label</a>'
            '<a href="https://example.com/guide">Useful link</a>'
        )
        rendered = sanitize_html(source)
        soup = BeautifulSoup(rendered, "html.parser")
        self.assertEqual(soup.h2.get_text(), "Useful heading")
        self.assertEqual(soup.strong.get_text(), "clear")
        self.assertEqual(soup.td["colspan"], "2")
        self.assertEqual(soup.find_all("a")[1]["href"], "https://example.com/guide")
        self.assertNotIn("href", soup.find_all("a")[0].attrs)
        for forbidden in ("script", "style", "iframe", "svg", "onclick", "onmouseover", "javascript:", "secret-script", "secret-iframe"):
            self.assertNotIn(forbidden, rendered)

    def test_obfuscated_urls_and_embedded_images_cannot_escape_allowlist(self):
        for url in ("jav&#x61;script:alert(1)", "java&#10;script:alert(1)", "data:text/html,<script>alert(1)</script>", "//evil.example/a", "https://user:password@example.com/a", "\\evil.example/a"):
            with self.subTest(url=url):
                soup = BeautifulSoup(sanitize_html(f'<p><a href="{url}">Read</a></p>'), "html.parser")
                self.assertNotIn("href", soup.a.attrs)
        cleaned = sanitize_html('<p>Image:</p><img src="https://example.com/tracker.png" onerror="alert(1)"><img src="data:image/svg+xml,invalid">')
        self.assertIsNone(BeautifulSoup(cleaned, "html.parser").find("img"))

    def test_owned_image_urls_are_normalized_and_attributes_escaped(self):
        image_id = uuid4()
        cleaned = sanitize_html(
            f'<img src="/superadmin/blog/media/{image_id}/" alt="&quot; onerror=&quot;alert(1)" '
            'width="999999" height="32" onerror="alert(1)">'
        )
        image = BeautifulSoup(cleaned, "html.parser").img
        self.assertEqual(image["src"], f"/blog/media/{image_id}/")
        self.assertEqual(image["width"], "4000")
        self.assertNotIn("onerror", image.attrs)
        self.assertEqual(image["alt"], '" onerror="alert(1)')

    def test_overlarge_html_is_rejected_before_parsing(self):
        with self.assertRaises(ValidationError):
            sanitize_html("a" * 300001)


class BlogMediaTests(BlogTestCase):
    def test_upload_decodes_real_image_resizes_and_stores_private_bytes(self):
        image = self.upload(size=(2400, 1200))
        self.assertEqual((image.width, image.height), (2000, 1000))
        self.assertEqual((image.thumbnail_width, image.thumbnail_height), (640, 320))
        with image.file.open("rb") as decoded:
            data = decoded.read()
        with Image.open(BytesIO(data)) as opened:
            self.assertEqual(opened.format, "WEBP")
            self.assertEqual(opened.size, (2000, 1000))
        with default_storage.open(image.file.name, "rb") as raw:
            encrypted = raw.read()
        self.assertTrue(encrypted.startswith(MAGIC))
        self.assertNotEqual(data, encrypted)
        self.assertEqual(self.client.get(image.public_url).status_code, 404)

    def test_upload_rejects_invalid_svg_and_excessive_bytes(self):
        uploads = (
            SimpleUploadedFile("not-an-image.png", b"not really a PNG", content_type="image/png"),
            SimpleUploadedFile("vector.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', content_type="image/svg+xml"),
            SimpleUploadedFile("huge.png", b"x" * (10 * 1024 * 1024 + 1), content_type="image/png"),
        )
        for upload in uploads:
            with self.subTest(filename=upload.name), self.assertRaises(ValidationError):
                services.upload_image(upload, actor=self.publisher)
        self.assertFalse(BlogImage.objects.exists())

    def test_missing_image_reference_cannot_be_published(self):
        for fields in (
            {"cover_image": str(uuid4())},
            {"social_image": str(uuid4())},
            {"body_html": f'<p>A guide.</p><img src="/blog/media/{uuid4()}/">'},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                post = self.post(**fields)
                self.publish(post)

    def test_inline_images_are_bound_to_publication_and_render_responsive_sizes(self):
        image = self.upload(size=(1400, 900))
        post = self.publish(body_html=f'<h2>Workflow example</h2><p>Here is the workflow.</p><img src="{image.preview_url}" alt="Workflow steps">')
        response = self.client.get(post.get_absolute_url())
        soup = BeautifulSoup(response.content, "html.parser")
        rendered = soup.find("img", src=image.public_url)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered["alt"], "Workflow steps")
        self.assertEqual(rendered["width"], "1400")
        self.assertEqual(rendered["loading"], "lazy")
        self.assertIn("640w", rendered["srcset"])
        self.assertTrue(image.live_posts.filter(pk=post.pk).exists())
        thumbnail = self.client.get(image.public_url, {"w": "640"})
        self.assertEqual(thumbnail.status_code, 200)
        bytes_ = b"".join(thumbnail.streaming_content) if thumbnail.streaming else thumbnail.content
        with Image.open(BytesIO(bytes_)) as opened:
            self.assertEqual(opened.width, 640)

    def test_image_type_is_verified_from_content_not_claimed_mime_type(self):
        genuine = image_upload()
        disguised = SimpleUploadedFile("image.png", genuine.read(), content_type="text/html")
        image = services.upload_image(disguised, actor=self.publisher)
        with image.file.open("rb") as output:
            with Image.open(output) as decoded:
                self.assertEqual(decoded.format, "WEBP")
