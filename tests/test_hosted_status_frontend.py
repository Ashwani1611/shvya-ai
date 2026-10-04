"""Run Hosted status polling regressions as part of application CI."""

from pathlib import Path
import subprocess


def test_hosted_status_polling_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--test", str(root / "tests/node/hosted-status.test.cjs")],
        cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
