from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "nginx" / "staging" / "default.conf"

MCP_PUBLIC_PATHS = [
    "/operations/mcp/",
    "/operations/oauth/register",
    "/operations/oauth/authorize",
    "/operations/oauth/token",
    "/operations/oauth/revoke",
    "/operations/.well-known/oauth-protected-resource",
    "/operations/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/operations",
]


def _config():
    return CONFIG_PATH.read_text(encoding="utf-8")


def _exact_location_body(config, path):
    pattern = re.compile(
        rf"location\s*=\s*{re.escape(path)}\s*\{{(?P<body>.*?)\n\s*\}}",
        re.DOTALL,
    )
    match = pattern.search(config)
    assert match, f"Missing exact staging Nginx location for {path}"
    return match.group("body")


def test_staging_keeps_global_basic_auth_enabled():
    config = _config()
    assert 'auth_basic "SHVYA AI Staging";' in config
    assert "auth_basic_user_file /etc/nginx/staging.htpasswd;" in config

    generic = re.search(
        r"location\s+/\s*\{(?P<body>.*?)\n\s*\}",
        config,
        re.DOTALL,
    )
    assert generic
    assert "auth_basic off;" not in generic.group("body")


def test_only_operations_mcp_protocol_paths_bypass_outer_basic_auth():
    config = _config()

    for path in MCP_PUBLIC_PATHS:
        body = _exact_location_body(config, path)
        assert "auth_basic off;" in body
        assert "proxy_pass http://web:8000;" in body
        assert "proxy_set_header Host staging.shvya-ai.com;" in body
        assert "proxy_set_header X-Forwarded-Proto https;" in body


def test_operations_mcp_basic_auth_bypass_is_exact_not_prefix_wide():
    config = _config()

    assert "location /operations/" not in config
    assert "location ^~ /operations/" not in config
    assert not re.search(
        r"location\s+~[^\n]*operations",
        config,
    )


def test_dashboard_and_superadmin_are_not_exempted_from_staging_basic_auth():
    config = _config()

    for path in (
        "/dashboard/",
        "/superadmin/",
        "/api/",
    ):
        assert f"location = {path}" not in config
        assert f"location {path}" not in config
