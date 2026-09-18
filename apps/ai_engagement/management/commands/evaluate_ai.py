from django.core.management.base import BaseCommand, CommandError

from apps.ai_engagement.evaluation.runner import run_evaluation


class Command(BaseCommand):
    help = "Run recorded AI behaviour regressions in pytest's test database (no live providers or delivery)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--scenarios", help="Optional declarative JSON scenario file")
        parser.add_argument("--output", default="ai-evaluation.json", help="JSON report destination")

    def handle(self, *args, **options):
        try:
            # Deliberately fixed: the command never inherits production DB/settings
            # as a test runner. pytest creates the isolated test_ database.
            report = run_evaluation(scenario_path=options["scenarios"], output=options["output"],
                                    settings_module="config.settings.testing")
        except (OSError, ValueError) as exc:
            raise CommandError(f"AI evaluation could not run: {exc}") from exc
        self.stdout.write(f"Passed: {report['passed']}  Failed: {report['failed']}  Skipped: {report['skipped']}")
        for category, count in report["failures_by_category"].items():
            self.stdout.write(f"{category.replace('_', ' ').title()} failures: {count}")
        self.stdout.write("Mode: recorded-provider backend regression; not a live language-model benchmark.")
        if report["failed"]:
            raise CommandError("AI regression checks failed. Promotion must remain blocked.")
