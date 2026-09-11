from pathlib import Path

from django.conf import settings


def test_sales_desk_loads_shared_premium_shell_assets():
    template = Path(settings.BASE_DIR, "templates", "copilot", "dashboard.html").read_text()

    assert 'include "base/premium_shell_assets.html"' in template
    assert 'include "base/base.html"' not in template
    assert "Sales Desk · SHVYA AI" in template


def test_premium_shell_hides_settings_from_sidebar_payload():
    template = Path(settings.BASE_DIR, "templates", "base", "premium_shell_assets.html").read_text()

    assert "item.label !== 'Settings'" in template
    assert "shvya-premium-shell" in template
    assert "shvya-sidebar-logo.svg" in template
