import subprocess
from pathlib import Path


def test_ai_brain_save_browser_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', '--test', str(root / 'tests/node/ai-brain-save.test.cjs')],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
