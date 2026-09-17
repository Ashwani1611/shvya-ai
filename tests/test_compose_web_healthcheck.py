from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _web_service_block(compose_path: Path) -> str:
    content = compose_path.read_text(encoding="utf-8")
    start = content.index("\n  web:\n")
    end = content.index("\n  ws:\n", start)
    return content[start:end]


@pytest.mark.parametrize(
    ("filename", "expected_host"),
    [
        ("docker-compose.yml", "dashboard.shvya-ai.com"),
        ("docker-compose.staging.yml", "staging.shvya-ai.com"),
    ],
)
def test_web_healthcheck_requires_dependency_readiness(filename, expected_host):
    block = _web_service_block(ROOT / filename)

    assert "/health/ready/" in block
    assert "r.status == 200" in block
    assert expected_host in block
    assert "X-Forwarded-Proto" in block


def test_production_web_healthcheck_does_not_accept_arbitrary_4xx():
    block = _web_service_block(ROOT / "docker-compose.yml")

    assert "100 <= r.status < 500" not in block
    assert "c.request('GET','/')" not in block
