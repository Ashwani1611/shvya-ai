import ast
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]



def _imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _application_python_files(root):
    for path in root.rglob("*.py"):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        yield path


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

    def test_domain_models_do_not_import_http_views(self):
        offenders = []
        for path in _application_python_files(ROOT / "apps"):
            if path.name != "models.py" and "models" not in path.parts:
                continue
            for module in _imported_modules(path):
                if ".views" in module or module.endswith(".views"):
                    offenders.append(f"{path.relative_to(ROOT)} -> {module}")
        self.assertEqual([], offenders, "Models must not depend on HTTP views.")

    def test_crm_does_not_depend_on_superadmin_or_operations_mcp(self):
        forbidden = ("apps.superadmin", "apps.integrations.operations")
        offenders = []
        for path in _application_python_files(ROOT / "apps" / "crm"):
            for module in _imported_modules(path):
                if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                    offenders.append(f"{path.relative_to(ROOT)} -> {module}")
        self.assertEqual(
            [],
            offenders,
            "CRM must remain independent from Superadmin and Operations MCP implementation.",
        )

    def test_channels_do_not_import_crm_views(self):
        offenders = []
        for path in _application_python_files(ROOT / "apps" / "channels"):
            for module in _imported_modules(path):
                if module == "apps.crm.views" or module.startswith("apps.crm.views."):
                    offenders.append(f"{path.relative_to(ROOT)} -> {module}")
        self.assertEqual([], offenders, "Channels must use CRM contracts/services, not CRM views.")

    def test_api_v1_has_one_canonical_composition_point(self):
        root_urls = (ROOT / "config" / "urls.py").read_text(encoding="utf-8")
        self.assertEqual(1, root_urls.count('include("api.v1.urls")'))
        self.assertFalse((ROOT / "api" / "v1" / "router.py").exists())

    def test_hosted_automation_registration_is_common(self):
        installed = [
            item
            for item in settings.INSTALLED_APPS
            if item.startswith("apps.hosted_automation")
        ]
        self.assertEqual(1, len(installed))
        for relative in (
            "config/settings/dev.py",
            "config/settings/testing.py",
            "config/settings/prod.py",
            "config/settings/staging.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn('"apps.hosted_automation"', source)

    def test_extended_operations_module_is_only_a_compatibility_facade(self):
        path = ROOT / "apps" / "integrations" / "operations_extended_tools.py"
        self.assertLess(
            len(path.read_text(encoding="utf-8").splitlines()),
            180,
            "Keep extended Operations implementation in domain tool modules.",
        )

    def test_operations_extended_tools_are_split_by_domain(self):
        base = ROOT / "apps" / "integrations" / "operations" / "tools"
        expected = {
            "qualification.py",
            "whatsapp.py",
            "workflows.py",
            "touchpoints.py",
            "faqs.py",
            "knowledge.py",
            "cadence.py",
            "simulations.py",
        }
        self.assertTrue(expected.issubset({path.name for path in base.glob("*.py")}))

