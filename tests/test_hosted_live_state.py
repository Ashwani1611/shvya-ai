"""Run the dependency-free Hosted live-state regressions in application CI."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_hosted_live_state_node_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed in this local Python environment")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests/test_hosted_live_state.cjs")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
