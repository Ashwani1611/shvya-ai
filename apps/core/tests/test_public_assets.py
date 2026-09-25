from pathlib import Path

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
        PUBLIC_ASSET_BASE_URL="https://assets.example.com/production/public/"
    )
    def test_public_asset_uses_external_origin_and_escapes_path(self):
        self.assertEqual(
            public_asset("marketing/demo film.mp4"),
            "https://assets.example.com/production/public/marketing/demo%20film.mp4",
        )

    def test_migration_manifest_sources_exist(self):
        static_root = Path(settings.BASE_DIR) / "static"
        missing = [
            relative
            for relative in HEAVY_PUBLIC_ASSETS
            if not (static_root / relative).is_file()
        ]
        self.assertEqual(missing, [])
