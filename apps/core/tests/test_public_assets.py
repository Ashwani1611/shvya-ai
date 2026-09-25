from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.core.public_assets import HEAVY_PUBLIC_ASSETS
from apps.core.templatetags.public_assets import public_asset


class PublicAssetTests(SimpleTestCase):
    @override_settings(PUBLIC_ASSET_BASE_URL="")
    def test_public_asset_falls_back_to_static(self):
        self.assertEqual(
            public_asset("marketing/example.mp4"),
            f"{settings.STATIC_URL}marketing/example.mp4",
        )

    @override_settings(
        PUBLIC_ASSET_BASE_URL="https://assets.example.com/production/media/public-assets/"
    )
    def test_public_asset_uses_external_origin_and_escapes_path(self):
        self.assertEqual(
            public_asset("marketing/demo film.mp4"),
            "https://assets.example.com/production/media/public-assets/marketing/demo%20film.mp4",
        )

    @override_settings(
        PUBLIC_ASSET_BASE_URL="",
        USE_S3_PUBLIC_ASSETS=True,
    )
    @patch("apps.core.templatetags.public_assets.storages")
    def test_public_asset_uses_s3_storage_when_enabled(self, storage_registry):
        storage_registry.__getitem__.return_value.url.return_value = (
            "https://signed-s3.example/object?signature=test"
        )
        self.assertEqual(
            public_asset("marketing/example.mp4"),
            "https://signed-s3.example/object?signature=test",
        )
        storage_registry.__getitem__.assert_called_once_with("public_assets")

    def test_migration_manifest_paths_are_safe_and_unique(self):
        self.assertEqual(len(HEAVY_PUBLIC_ASSETS), len(set(HEAVY_PUBLIC_ASSETS)))
        for relative in HEAVY_PUBLIC_ASSETS:
            self.assertTrue(relative)
            self.assertFalse(relative.startswith("/"))
            self.assertNotIn("..", relative.split("/"))
