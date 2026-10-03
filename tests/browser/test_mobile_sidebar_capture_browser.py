"""Mobile/search capture-order regression, with real production listener code.

The command center UI is stubbed; its document-capture listeners are extracted
from the checked-in production controller, not reimplemented as bubble handlers.
This is not an authenticated page, whole-command-center or visual-layout test.
"""
import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
HTML = """<!doctype html><html class="shvya-premium-shell"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body { margin: 0; font: 16px system-ui; }
button, input { min-height: 44px; }
#app-sidebar { position: fixed; left: 0; top: 0; width: 250px; height: 100%;
    background: white; z-index: 20; visibility: hidden; }
.shvya-mobile-sidebar-open #app-sidebar { visibility: visible; }
.shvya-mobile-sidebar-backdrop { position: fixed; inset: 0; z-index: 10;
    visibility: hidden; pointer-events: none; }
.shvya-mobile-sidebar-open .shvya-mobile-sidebar-backdrop {
    visibility: visible; pointer-events: auto; }
#search-panel { position: fixed; inset: 30px; z-index: 30; background: white; }
@media(min-width:901px) {
    #app-sidebar { position: static; visibility: visible; }
    .shvya-mobile-sidebar-trigger { display: none; }
}
</style></head><body>
<div><aside id="app-sidebar"><button data-shvya-sidebar-toggle>Close</button>
<button class="shvya-search-trigger"><span>Search workspace</span></button>
<a href="#crm">CRM</a></aside><div><header><h1>Workspace</h1><div></div></header>
<main><input aria-label="Lead name"></main></div></div>
<section id="search-panel" hidden><input aria-label="Command search"></section>
<div id="preserve-inert" inert>Other module's inactive panel</div>
</body></html>"""


def capture_listeners() -> str:
    """Fail explicitly when production registration changes; never silently mock it."""
    source = (ROOT / "static/js/shvya_command_center.js").read_text(encoding="utf-8")
    start = source.rfind("        document.addEventListener('click', function (event) {")
    end = source.find("\n    }\n\n    if (document.readyState", start)
    assert start >= 0 and end > start, "Review command-center listener extraction after controller changes."
    listeners = source[start:end]
    assert "event.stopImmediatePropagation()" in listeners
    assert "document.addEventListener('keydown'" in listeners
    return listeners


@pytest.fixture(scope="module")
def capture_browser():
    with sync_playwright() as playwright:
        options = {"headless": True}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable:
            options["executable_path"] = executable
        try:
            browser = playwright.chromium.launch(**options)
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed.")
            raise
        yield browser
        browser.close()


def mount(capture_browser, *, width=390, collapsed=False):
    page = capture_browser.new_page(viewport={"width": width, "height": 844})
    page.set_default_timeout(2000)
    page.set_content(HTML)
    page.evaluate("""collapsed => {
        // about:blank has no storage origin; retain the same preference contract.
        const preferences = new Map();
        Object.defineProperty(window, 'localStorage', {value: {
            getItem: key => preferences.get(key) ?? null,
            setItem: (key, value) => preferences.set(key, String(value))
        }, configurable: true});
        document.querySelector('#app-sidebar').classList.toggle('sidebar-collapsed', collapsed);
        localStorage.setItem('shvya-sidebar-collapsed', collapsed ? '1' : '0');
    }""", collapsed)
    # Deliberately register the real center's capture listeners FIRST, matching
    # premium_shell_assets.html. A bubble-only mock cannot catch this regression.
    page.add_script_tag(content="""(function () {
        var panel = document.getElementById('search-panel');
        var input = panel.querySelector('input');
        var returnFocus;
        var center = {
            open: function () {
                returnFocus = document.activeElement;
                panel.hidden = false;
                input.focus();
            },
            close: function () {
                panel.hidden = true;
                if (returnFocus) returnFocus.focus();
            },
            isOpen: function () { return !panel.hidden; }
        };
    """ + capture_listeners() + "\n})();")
    page.add_script_tag(path=str(ROOT / "static/js/shvya_premium_sidebar_mobile.js"))
    return page


@pytest.mark.parametrize("activation", ["click", "Control+k", "Meta+k"])
@pytest.mark.parametrize("collapsed", [False, True])
def test_search_releases_drawer_before_document_capture(capture_browser, activation, collapsed):
    page = mount(capture_browser, collapsed=collapsed)
    try:
        for _ in range(2):
            page.get_by_role("button", name="Open navigation", exact=True).click()
            expect(page.locator("#search-panel")).to_have_attribute("inert", "")
            if activation == "click":
                page.locator(".shvya-search-trigger span").click()
            else:
                page.keyboard.press(activation)
            expect(page.locator("#app-sidebar")).to_have_attribute("aria-hidden", "true", timeout=2000)
            expect(page.get_by_role("textbox", name="Command search")).to_be_focused()
            expect(page.locator("#search-panel")).not_to_have_attribute("inert", "")
            page.get_by_role("textbox", name="Command search").fill("CRM")
            expect(page.get_by_role("textbox", name="Command search")).to_have_value("CRM")
            page.keyboard.press("Escape")
            expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_focused()
            expect(page.locator("#preserve-inert")).to_have_attribute("inert", "")
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == str(int(collapsed))
        assert page.locator("#app-sidebar").evaluate("e => e.classList.contains('sidebar-collapsed')") == collapsed
    finally:
        page.close()


@pytest.mark.parametrize("activation", ["click", "Control+k"])
def test_desktop_search_still_uses_existing_center(capture_browser, activation):
    page = mount(capture_browser, width=1280)
    try:
        if activation == "click":
            page.locator(".shvya-search-trigger").click()
        else:
            page.keyboard.press(activation)
        expect(page.get_by_role("textbox", name="Command search")).to_be_focused()
        assert not page.locator("#app-sidebar").evaluate("e => e.inert")
        page.keyboard.press("Escape")
        expect(page.locator("#search-panel")).to_be_hidden()
    finally:
        page.close()
