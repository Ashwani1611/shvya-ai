from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTests(SimpleTestCase):
    def test_crm_dashboard_does_not_regrow_focused_workflows(self):
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

    def test_retired_ai_patch_modules_stay_removed(self):
        for relative in (
            "apps/ai_engagement/services/ai_setup_runtime_fixes.py",
            "apps/ai_engagement/services/crm_action_projection_fix.py",
            "apps/ai_engagement/services/customer_chat_regressions.py",
            "apps/ai_engagement/services/phase5_6_safety_fixes.py",
            "apps/ai_engagement/services/qualification_crm_action_runtime.py",
        ):
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())
