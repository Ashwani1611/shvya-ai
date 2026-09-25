import mimetypes
from pathlib import Path

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.public_assets import HEAVY_PUBLIC_ASSETS


class Command(BaseCommand):
    help = "Upload SHVYA heavy public static binaries to the configured S3 prefix."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List objects that would be uploaded without writing to S3.",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify every configured object already exists in S3.",
        )

    def handle(self, *args, **options):
        bucket = str(
            getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or ""
        ).strip()
        prefix = str(
            getattr(
                settings,
                "AWS_S3_PUBLIC_ASSET_PREFIX",
                "production/media/public-assets",
            )
            or ""
        ).strip().strip("/")
        region = str(
            getattr(settings, "AWS_S3_REGION_NAME", "") or ""
        ).strip() or None

        if not bucket:
            raise CommandError("AWS_STORAGE_BUCKET_NAME is required.")

        client = boto3.client("s3", region_name=region)
        static_root = Path(settings.BASE_DIR) / "static"
        processed = 0

        for relative in HEAVY_PUBLIC_ASSETS:
            key = f"{prefix}/{relative}" if prefix else relative

            if options["verify"]:
                try:
                    client.head_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    raise CommandError(
                        f"Missing S3 object s3://{bucket}/{key}: {exc}"
                    ) from exc
                self.stdout.write(
                    self.style.SUCCESS(f"verified s3://{bucket}/{key}")
                )
                processed += 1
                continue

            source = static_root / relative
            if not source.is_file():
                raise CommandError(f"Source asset is missing: {source}")

            if options["dry_run"]:
                self.stdout.write(
                    f"would upload {source} -> s3://{bucket}/{key}"
                )
                continue

            content_type = (
                mimetypes.guess_type(source.name)[0]
                or "application/octet-stream"
            )
            client.upload_file(
                str(source),
                bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public,max-age=31536000,immutable",
                    "ServerSideEncryption": "AES256",
                },
            )
            self.stdout.write(
                self.style.SUCCESS(f"uploaded s3://{bucket}/{key}")
            )
            processed += 1

        if options["verify"]:
            self.stdout.write(
                self.style.SUCCESS(f"Verified {processed} public assets.")
            )
        elif not options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"Uploaded {processed} public assets.")
            )
