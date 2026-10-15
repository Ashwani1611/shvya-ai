from io import BytesIO
from struct import pack
from unittest.mock import patch
from zlib import crc32

from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.urls import reverse
from PIL import Image

from apps.blog import services
from apps.blog.models import BlogImage
from apps.blog.storage import MAGIC
from apps.blog.tests.helpers import BlogTestCase, authenticate_area, image_upload


def s3_error(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": "private-storage-credential"}}, "GetObject")


class BlogUploadResilienceTests(BlogTestCase):
    def setUp(self):
        super().setUp()
        authenticate_area(self.client, self.publisher)
        self.upload_url = reverse("blog_admin:upload_image")

    def test_supported_formats_upload_and_grayscale_transparency_survives(self):
        for image_format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                response = self.client.post(self.upload_url, {"image": image_upload(image_format=image_format)})
                self.assertEqual(response.status_code, 201)
                image = BlogImage.objects.get(pk=response.json()["id"])
                with image.file.open("rb") as stored, Image.open(stored) as decoded:
                    self.assertEqual(decoded.format, "WEBP")
        output = BytesIO()
        Image.new("LA", (8, 8), (100, 0)).save(output, "PNG")
        image = services.upload_image(SimpleUploadedFile("transparent.png", output.getvalue()), self.publisher)
        with image.file.open("rb") as stored, Image.open(stored) as decoded:
            self.assertEqual(decoded.getpixel((0, 0))[3], 0)

    def test_corrupt_png_crc_and_truncated_files_return_field_guidance(self):
        data = bytearray(image_upload().read())
        offset = data.index(b"IDAT")
        length = int.from_bytes(data[offset - 4:offset], "big")
        data[offset + 4 + length] ^= 1  # Break the checksum without changing decoded pixels.
        uploads = [bytes(data), image_upload(image_format="JPEG").read()[:50], b""]
        for payload in uploads:
            with self.subTest(size=len(payload)):
                response = self.client.post(self.upload_url, {"image": SimpleUploadedFile("broken.png", payload)})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
                self.assertIn("valid JPEG, PNG or WebP", response.json()["detail"])
        self.assertFalse(BlogImage.objects.exists())

    def test_dimension_limit_is_checked_before_pixel_decode(self):
        data = bytearray(image_upload().read())
        data[16:24] = pack(">II", 6000, 6000)
        data[29:33] = pack(">I", crc32(data[12:29]))
        with self.assertRaisesMessage(ValidationError, "25 megapixels"):
            services.upload_image(SimpleUploadedFile("oversized.png", bytes(data)), self.publisher)
        self.assertFalse(BlogImage.objects.exists())

    def test_decompression_warnings_and_animated_images_are_rejected(self):
        with patch("PIL.Image.MAX_IMAGE_PIXELS", 1000), self.assertRaises(ValidationError):
            services.upload_image(image_upload(), self.publisher)
        output = BytesIO()
        Image.new("RGB", (8, 8), "red").save(
            output, "PNG", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")], duration=100,
        )
        with self.assertRaisesMessage(ValidationError, "Animated images"):
            services.upload_image(SimpleUploadedFile("animation.png", output.getvalue()), self.publisher)
        self.assertFalse(BlogImage.objects.exists())

    def test_stream_read_and_untrusted_size_cannot_bypass_validation(self):
        upload = image_upload()
        with patch.object(upload.file, "read", side_effect=OSError("private-temp-path")):
            with self.assertRaisesMessage(ValidationError, "Choose the image again"):
                services.upload_image(upload, self.publisher)
        upload = SimpleUploadedFile("oversized.png", b"x" * (services.MAX_UPLOAD_BYTES + 1))
        upload.size = 0
        with self.assertRaisesMessage(ValidationError, "10 MB"):
            services.upload_image(upload, self.publisher)
        self.assertFalse(BlogImage.objects.exists())

    def test_storage_failures_return_retryable_json_without_provider_details(self):
        errors = [s3_error(), EndpointConnectionError(endpoint_url="https://private-storage-credential.invalid"),
                  OSError("private-storage-credential")]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch(
                "apps.blog.storage.default_storage.save", side_effect=error,
            ), self.assertLogs("apps.blog.services", level="ERROR") as logs:
                response = self.client.post(self.upload_url, {"image": image_upload()})
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()["ok"])
                self.assertIn("retry", response.json()["detail"])
                self.assertEqual(response["Retry-After"], "60")
                self.assertIn("no-store", response["Cache-Control"])
                self.assertNotIn("private-storage-credential", response.content.decode())
                self.assertNotIn("private-storage-credential", str([record.__dict__ for record in logs.records]))
                if isinstance(error, ClientError):
                    self.assertEqual(logs.records[-1].storage_code, "AccessDenied")
        self.assertFalse(BlogImage.objects.exists())

    def test_thumbnail_save_failure_removes_the_completed_large_image(self):
        save = default_storage.save
        saved_names = []

        def save_then_fail(name, content, **kwargs):
            if saved_names:
                raise s3_error()
            saved_names.append(save(name, content, **kwargs))
            return saved_names[-1]

        with patch("apps.blog.storage.default_storage.save", side_effect=save_then_fail):
            response = self.client.post(self.upload_url, {"image": image_upload()})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(saved_names), 1)
        self.assertFalse(default_storage.exists(saved_names[0]))
        self.assertFalse(BlogImage.objects.exists())

    def test_cleanup_failure_does_not_hide_db_error_or_skip_other_object(self):
        delete = default_storage.delete
        attempted = []

        def fail_first_delete(name):
            attempted.append(name)
            if len(attempted) == 1:
                raise OSError("private-storage-credential")
            return delete(name)

        with patch.object(BlogImage, "save", side_effect=OperationalError("private-database-credential")), patch(
            "apps.blog.storage.default_storage.delete", side_effect=fail_first_delete,
        ), self.assertLogs("apps.blog.services", level="ERROR") as logs:
            response = self.client.post(self.upload_url, {"image": image_upload()})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(attempted), 2)
        self.assertTrue(default_storage.exists(attempted[0]))
        self.assertFalse(default_storage.exists(attempted[1]))
        self.assertIn("cleanup failed", str(logs.output))
        self.assertNotIn("credential", str([record.__dict__ for record in logs.records]))
        self.assertNotIn("credential", response.content.decode())
        self.assertFalse(BlogImage.objects.exists())

    def test_unexpected_programming_errors_are_not_misreported_as_storage_outages(self):
        with patch.object(BlogImage, "save", side_effect=RuntimeError("Unexpected application error")):
            with self.assertRaisesMessage(RuntimeError, "Unexpected application error"):
                services.upload_image(image_upload(), self.publisher)
        self.assertEqual(default_storage.listdir("blog-images")[1], [])


class BlogMediaFailureTests(BlogTestCase):
    def setUp(self):
        super().setUp()
        authenticate_area(self.client, self.publisher)
        self.image = self.upload()
        self.publish(cover_image=str(self.image.pk))

    def assert_safe_unavailable(self, url, status):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("private-storage-credential", response.content.decode())
        if status == 503:
            self.assertEqual(response["Retry-After"], "60")
        return response

    def test_missing_and_corrupt_local_objects_are_controlled_not_server_errors(self):
        default_storage.delete(self.image.file.name)
        for url in (self.image.public_url, self.image.preview_url):
            with self.subTest(url=url):
                self.assert_safe_unavailable(url, 404)
        for damaged in (b"invalid-encrypted-image", MAGIC + b"invalid-authentication-token"):
            default_storage.delete(self.image.file.name)
            default_storage.save(self.image.file.name, ContentFile(damaged))
            for url in (self.image.public_url, self.image.preview_url):
                with self.subTest(url=url, damaged=damaged):
                    self.assert_safe_unavailable(url, 404)

    def test_missing_thumbnail_falls_back_to_authorized_original(self):
        default_storage.delete(self.image.thumbnail_file.name)
        for url in (self.image.public_url, self.image.preview_url):
            with self.subTest(url=url):
                response = self.client.get(url, {"w": "640"})
                self.assertEqual(response.status_code, 200)
                data = b"".join(response.streaming_content)
                with Image.open(BytesIO(data)) as decoded:
                    self.assertEqual(decoded.size, (self.image.width, self.image.height))

    def test_s3_missing_and_unavailable_objects_have_distinct_statuses(self):
        errors = [(s3_error("NoSuchKey"), 404), (s3_error("AccessDenied"), 503),
                  (EndpointConnectionError(endpoint_url="https://private-storage-credential.invalid"), 503)]
        for error, status in errors:
            for url in (self.image.public_url, self.image.preview_url):
                with self.subTest(error=type(error).__name__, status=status, url=url), patch(
                    "apps.blog.storage.default_storage.open", side_effect=error,
                ), self.assertLogs("apps.blog.services", level="WARNING") as logs:
                    self.assert_safe_unavailable(url, status)
                    self.assertNotIn("private-storage-credential", str([record.__dict__ for record in logs.records]))

    def test_private_only_images_never_attempt_public_storage_access(self):
        private_image = self.upload()
        with patch("apps.blog.storage.default_storage.open") as opened:
            self.assertEqual(self.client.get(private_image.public_url).status_code, 404)
        opened.assert_not_called()
