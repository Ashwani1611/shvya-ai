"""Publish the checked-in Android binary only after its public bytes verify."""

import hashlib
from pathlib import Path

import requests
from django.conf import settings
from django.db import transaction

from apps.telephony.models import CallAppRelease


APK_PATH = "downloads/SHVYA-Call-Intelligence-v1.6-preview.apk"
APK_SHA256 = "a9e271d9637214db2a91048f56691ebb8cb3ffc3b9c58653c1823e06007c2437"
APK_VERSION_CODE = 10
APK_VERSION_NAME = "1.6 Preview"
APK_RELEASE_NOTES = (
    "Improved sign-in stability, sync, reminders and refreshed UI. "
    "Installs as SHVYA Preview alongside the previous app. "
    "Use only one app for call tracking."
)


def publish_android_release():
    """Verify the deployed file, then atomically select it for the download button.

    Production deployments are serialized by the deploy workflow. A later release
    is never downgraded, and existing release history is retained.
    """
    if CallAppRelease.objects.filter(is_active=True, version_code__gt=APK_VERSION_CODE).exists():
        return CallAppRelease.objects.filter(is_active=True).first()

    source = Path(settings.BASE_DIR) / "static" / APK_PATH
    if hashlib.sha256(source.read_bytes()).hexdigest() != APK_SHA256:
        raise ValueError("Bundled Android APK checksum mismatch; release was not changed.")

    origin = settings.OPERATIONS_PUBLIC_BASE_URL.rstrip("/")
    if not origin.startswith("https://"):
        raise ValueError("Android publication requires an HTTPS deployment origin.")
    url = f"{origin}/static/{APK_PATH}"
    digest = hashlib.sha256()
    size = 0
    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > source.stat().st_size:
                raise ValueError("Public Android APK size mismatch; release was not changed.")
            digest.update(chunk)
    if digest.hexdigest() != APK_SHA256:
        raise ValueError("Public Android APK checksum mismatch; release was not changed.")

    with transaction.atomic():
        releases = list(CallAppRelease.objects.select_for_update().all())
        newer = next((r for r in releases if r.is_active and r.version_code > APK_VERSION_CODE), None)
        if newer:
            return newer
        release = next((r for r in releases if r.version_code == APK_VERSION_CODE), None)
        if release and release.sha256 and release.sha256 != APK_SHA256:
            raise ValueError("Android version code already belongs to another APK.")
        release, _ = CallAppRelease.objects.update_or_create(
            version_code=APK_VERSION_CODE,
            defaults={
                "version_name": APK_VERSION_NAME,
                "download_url": url,
                "sha256": APK_SHA256,
                "min_android_sdk": 23,
                "release_notes": APK_RELEASE_NOTES,
                "is_active": True,
                "is_mandatory": False,
            },
        )
        CallAppRelease.objects.filter(is_active=True).exclude(pk=release.pk).update(is_active=False)
    return release
