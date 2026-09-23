from pathlib import Path

from django.test import SimpleTestCase

from apps.ai_engagement.services import qualification_execution_contract as contract


ROOT = Path(__file__).resolve().parents[3]


class QualificationExecutionArchitectureTests(SimpleTestCase):
    def test_historical_contract_stays_a_small_runtime_facade(self):
        path = (
            ROOT
            / "apps"
            / "ai_engagement"
            / "services"
            / "qualification_execution_contract.py"
        )
        self.assertLess(
            len(path.read_text(encoding="utf-8").splitlines()),
            350,
            "Keep qualification execution logic in focused components.",
        )

    def test_authoritative_components_exist(self):
        base = ROOT / "apps" / "ai_engagement" / "services" / "qualification_execution"
        expected = {
            "common.py",
            "config.py",
            "completion.py",
            "planning.py",
            "evidence.py",
            "executor.py",
            "reconciliation.py",
            "finalization.py",
        }
        self.assertTrue(expected.issubset({path.name for path in base.glob("*.py")}))

    def test_compatibility_contract_exports_runtime_entrypoints(self):
        self.assertTrue(callable(contract.resolve_before_generation))
        self.assertTrue(callable(contract.install_qualification_execution_contract))
        self.assertTrue(callable(contract._config))
        self.assertTrue(callable(contract._completion_target))
