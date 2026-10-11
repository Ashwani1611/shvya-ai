from django.core.management.base import BaseCommand, CommandError

from apps.telephony.releases import publish_android_release


class Command(BaseCommand):
    help = "Verify and publish the bundled Android APK to Call Intelligence."

    def handle(self, *args, **options):
        try:
            release = publish_android_release()
        except Exception as exc:
            raise CommandError(f"Android release publication failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Active Android release: {release.version_name} ({release.version_code}) "
            f"url={release.download_url} sha256={release.sha256}"
        ))
