from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTests(SimpleTestCase):
    def test_operations_tool_facade_stays_focused(self):
        path = ROOT / "apps" / "integrations" / "operations_tools.py"
        self.assertLess(
            len(path.read_text(encoding="utf-8").splitlines()),
            1200,
            "operations_tools.py is a compatibility facade; add tools to focused modules.",
        )

    def test_crm_dashboard_does_not_regrow_import_and_reminder_workflows(self):
        path = ROOT / "apps" / "crm" / "views" / "dashboard.py"
        self.assertLess(
            len(path.read_text(encoding="utf-8").splitlines()),
            3500,
            "Move focused CRM workflows to their owning view modules.",
        )

    def test_legacy_calls_app_is_not_installed(self):
        self.assertNotIn("apps.calls", settings.INSTALLED_APPS)
        self.assertFalse((ROOT / "apps" / "calls").exists())

    def test_obsolete_architecture_scaffolding_stays_removed(self):
        obsolete = (
            "coverage_now.txt",
            "coverage_baseline.txt",
            "fix_arch.sh",
            "migrate_files.sh",
            "restructure_step1.sh",
            "restructure_step2.sh",
            "setup_architecture.sh",
        )
        for relative in obsolete:
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())

    def test_empty_parallel_root_service_trees_stay_removed(self):
        for relative in (
            "services/ai",
            "services/knowledge",
            "services/notifications",
            "services/telephony",
        ):
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())
