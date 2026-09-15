from pathlib import Path

from django.conf import settings


def _project_file(relative_path):
    return (Path(settings.BASE_DIR) / relative_path).read_text(encoding="utf-8")


def test_premium_shell_loads_advanced_command_center_assets():
    template = _project_file("templates/base/premium_shell_assets.html")

    assert "css/shvya_command_center.css" in template
    assert "js/shvya_command_center.js" in template


def test_command_center_registers_core_dashboard_actions():
    source = _project_file("static/js/shvya_command_center.js")

    for expected_action in (
        "Create cadence sequence",
        "Create workflow",
        "Create WhatsApp template",
        "Hosted chats",
        "Coexistence chats",
        "WhatsApp API / AI chats",
        "Instagram chats",
        "Open settings",
    ):
        assert expected_action in source

    assert "discoverPageActions" in source
    assert "buildSearchActions" in source
    assert "shvya_action" in source
    assert "event.stopImmediatePropagation()" in source
