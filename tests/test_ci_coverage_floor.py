import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MINIMUM_COVERAGE = 60.0


def test_ci_enforces_meaningful_coverage_floor():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    configured_floor = float(pyproject["tool"]["coverage"]["report"]["fail_under"])

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"--cov-fail-under=(\d+(?:\.\d+)?)", workflow)

    assert match is not None, "CI must explicitly enforce a coverage floor."
    ci_floor = float(match.group(1))

    assert configured_floor >= MINIMUM_COVERAGE
    assert ci_floor == configured_floor
