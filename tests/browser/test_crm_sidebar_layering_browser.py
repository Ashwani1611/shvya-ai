"""Hit-testing regression for the CRM-only fixed shell and mobile drawer.

Uses the production mobile controller, both production mobile stylesheets, and
inline styles extracted from the actual CRM template. Utility layout/navigation
markup and the search UI are fixtures: not authenticated or full-page coverage.
No forced clicks or DOM-dispatched clicks: an overlay must not hide the controls.
"""
from pathlib import Path
import os
import re

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
MOBILE_JS = ROOT / "static/js/shvya_premium_sidebar_mobile.js"
MOBILE_CSS = (
    ROOT / "static/css/shvya_premium_sidebar_mobile.css",
    ROOT / "static/css/shvya_premium_shell_mobile_patch.css",
)
# Only utility/desktop layout is approximated. Layer ordering, drawer positioning,
# backdrop visibility and CRM's stacking context come from production assets.
FIXTURE_HTML = """<!doctype html><html class="shvya-premium-shell"><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0;overflow:hidden;font:14px system-ui}
.flex{display:flex}.h-screen{height:100dvh}.overflow-hidden{overflow:hidden}
#app-sidebar{width:260px;flex:0 0 260px;position:relative;overflow:hidden}
#app-sidebar.sidebar-collapsed{width:72px;flex-basis:72px}
.shvya-sidebar-shell{height:100%;min-height:0;display:flex;flex-direction:column;padding:12px}
.shvya-sidebar-brand{display:flex;align-items:center;flex:none}.shvya-brand-name{flex:1}
.shvya-search-trigger{display:flex;align-items:center}.shvya-sidebar-scroll{flex:1;min-height:0;overflow:auto}
.shvya-nav-stack,.shvya-nav-children{display:flex;flex-direction:column}
.shvya-nav-row{display:flex;align-items:center;min-height:42px;text-decoration:none;color:inherit}
.shvya-nav-children-grid{display:grid;grid-template-rows:0fr;opacity:0}
.shvya-nav-group.is-open>.shvya-nav-children-grid{grid-template-rows:1fr;opacity:1}
.shvya-nav-children{min-height:0;overflow:hidden}.shvya-sidebar-footer{flex:none}
.shvya-profile-card{display:flex;align-items:center;min-height:48px}
#workspace{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0}
header{display:flex;align-items:center;flex:none}header>div{display:flex}
main{flex:1;min-height:0;overflow:auto}main article{height:1600px}
#search-dialog{display:none;position:fixed;inset:20px;z-index:200;background:white}
#search-dialog.is-open{display:block}
</style></head><body>
<div id="app-shell" class="flex h-screen overflow-hidden">
<aside id="app-sidebar" class="shvya-premium-sidebar-ready">
<div class="shvya-sidebar-shell">
<div class="shvya-sidebar-brand"><span class="shvya-brand-name">SHVYA AI</span>
<button type="button" class="shvya-sidebar-toggle" data-shvya-sidebar-toggle aria-label="Collapse sidebar"><i class="ti"></i></button></div>
<button type="button" class="shvya-search-trigger">Search or jump to</button>
<div class="shvya-sidebar-scroll"><div class="shvya-nav-stack">
<a class="shvya-nav-row" href="#sales-desk"><span class="shvya-nav-copy">Sales Desk</span></a>
<div class="shvya-nav-group"><button type="button" class="shvya-nav-row" aria-expanded="false">WhatsApp</button>
<div class="shvya-nav-children-grid"><div class="shvya-nav-children">
<a class="shvya-nav-row" href="#api-accounts">API accounts</a></div></div></div>
</div></div>
<div class="shvya-sidebar-footer"><a class="shvya-profile-card" href="#profile">Profile</a></div>
</div></aside>
<div id="workspace"><header><h1>CRM</h1><div><button type="button">New lead</button></div></header>
<main><article><button id="content-action" type="button">Workspace action</button></article></main></div>
</div>
<div id="already-inert" inert>Previously disabled content</div>
<div id="search-dialog"><input aria-label="Search workspace"><button type="button" id="close-search">Close search</button></div>
<script>
// Stub only the search UI. Its capture/stopImmediatePropagation behaviour
// matches the command center; the mobile controller itself is not mocked.
var palette=document.getElementById('search-dialog');var returnFocus;
function openSearch(){returnFocus=document.activeElement;palette.classList.add('is-open');palette.querySelector('input').focus();}
function closeSearch(){palette.classList.remove('is-open');if(returnFocus)returnFocus.focus();}
document.getElementById('close-search').addEventListener('click',closeSearch);
document.addEventListener('click',function(e){if(e.target.closest('.shvya-search-trigger')){e.preventDefault();e.stopImmediatePropagation();openSearch();}},true);
document.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();e.stopImmediatePropagation();openSearch();}else if(e.key==='Escape'&&palette.classList.contains('is-open')){e.preventDefault();e.stopImmediatePropagation();closeSearch();}},true);
window.desktopToggleCalls=0;
document.querySelector('[data-shvya-sidebar-toggle]').addEventListener('click',function(){
window.desktopToggleCalls++;var node=document.getElementById('app-sidebar');node.classList.toggle('sidebar-collapsed');
localStorage.setItem('shvya-sidebar-collapsed',node.classList.contains('sidebar-collapsed')?'1':'0');});
</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        options = {"headless": True}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable:
            options["executable_path"] = executable
        try:
            instance = playwright.chromium.launch(**options)
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed.")
            raise
        try:
            yield instance
        finally:
            instance.close()


def mount(browser, *, crm=True, collapsed=False, size=(390, 844)):
    context = browser.new_context(
        viewport={"width": size[0], "height": size[1]}, has_touch=True,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.set_default_timeout(3000)
    page.set_content(FIXTURE_HTML)
    # about:blank has no storage origin; mock only the preference store.
    page.evaluate("""() => {
        const preferences = new Map();
        Object.defineProperty(window, 'localStorage', {value: {
            getItem: key => preferences.get(key) ?? null,
            setItem: (key, value) => preferences.set(key, String(value))
        }, configurable: true});
    }""")
    for css in MOBILE_CSS:
        page.add_style_tag(path=str(css))
    if crm:
        template = (ROOT / "templates/crm/dashboard.html").read_text(encoding="utf-8")
        styles = re.findall(r"<style[^>]*>(.*?)</style>", template, flags=re.S)
        assert styles, "Load the real CRM-specific CSS rather than a generic shell only"
        # The template's extra_head follows block.super in production as well.
        for style in styles:
            page.add_style_tag(content=style)
    page.evaluate("""collapsed => {
        localStorage.setItem('shvya-sidebar-collapsed',collapsed?'1':'0');
        document.getElementById('app-sidebar').classList.toggle('sidebar-collapsed',collapsed);
        document.documentElement.classList.toggle('shvya-sidebar-pref-collapsed',collapsed);
    }""", collapsed)
    page.add_script_tag(path=str(MOBILE_JS))
    return context, page


def hit_target(locator):
    """Visibility alone passes even when the backdrop steals every pointer hit."""
    locator.scroll_into_view_if_needed()
    result = locator.evaluate("""node => {
        const r=node.getBoundingClientRect();
        const target=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
        return {ok:target===node||node.contains(target),hit:target&&target.outerHTML.slice(0,220)};
    }""")
    assert result["ok"], result["hit"]


@pytest.mark.parametrize("crm", [False, True], ids=["standard", "crm"])
@pytest.mark.parametrize("collapsed", [False, True], ids=["expanded", "compact"])
@pytest.mark.parametrize("size", [(320, 568), (390, 844), (844, 390)])
@pytest.mark.parametrize("gesture", ["click", "tap"])
def test_drawer_controls_receive_pointer_hits(browser, crm, collapsed, size, gesture):
    context, page = mount(browser, crm=crm, collapsed=collapsed, size=size)
    try:
        trigger = page.locator(".shvya-mobile-sidebar-trigger")
        getattr(trigger, gesture)()
        expect(page.locator("#app-sidebar")).to_have_attribute("aria-modal", "true")
        close = page.locator("[data-shvya-sidebar-toggle]")
        hit_target(close)
        assert page.locator("#app-sidebar+div").get_attribute("id") == "workspace"
        expect(page.locator("#workspace")).to_have_attribute("inert", "")
        assert page.locator("main").evaluate("node=>getComputedStyle(node).overflowY") == "hidden"
        group = page.locator(".shvya-nav-group>button")
        hit_target(group)
        getattr(group, gesture)()
        expect(group).to_have_attribute("aria-expanded", "true")
        child = page.get_by_role("link", name="API accounts", exact=True)
        hit_target(child)
        getattr(child, gesture)()
        expect(page).to_have_url(re.compile(r"#api-accounts$"))
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        getattr(trigger, gesture)()
        direct = page.get_by_role("link", name="Sales Desk", exact=True)
        hit_target(direct)
        getattr(direct, gesture)()
        expect(page).to_have_url(re.compile(r"#sales-desk$"))
        getattr(trigger, gesture)()
        hit_target(close)
        getattr(close, gesture)()
        expect(trigger).to_be_focused()
        expect(page.locator("#workspace")).not_to_have_attribute("inert", "")
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == str(int(collapsed))
        assert page.evaluate("window.desktopToggleCalls") == 0
        expect(page.locator("#already-inert")).to_have_attribute("inert", "")
    finally:
        context.close()


@pytest.mark.parametrize("crm", [False, True], ids=["standard", "crm"])
@pytest.mark.parametrize("method", ["click", "tap", "keyboard"])
def test_search_handoff_after_drawer_reparenting(browser, crm, method):
    context, page = mount(browser, crm=crm)
    try:
        trigger = page.locator(".shvya-mobile-sidebar-trigger")
        trigger.click()
        if method == "keyboard":
            page.keyboard.press("Control+k")
        else:
            search = page.locator(".shvya-search-trigger")
            hit_target(search)
            getattr(search, method)()
        expect(page.get_by_role("textbox", name="Search workspace")).to_be_focused()
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        page.get_by_role("textbox", name="Search workspace").fill("CRM")
        page.get_by_role("button", name="Close search", exact=True).click()
        expect(trigger).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize("crm", [False, True], ids=["standard", "crm"])
@pytest.mark.parametrize("method", ["backdrop", "escape", "history", "resize"])
def test_close_cleanup_and_repeat_open(browser, crm, method):
    context, page = mount(browser, crm=crm)
    try:
        trigger = page.locator(".shvya-mobile-sidebar-trigger")
        page.add_script_tag(path=str(MOBILE_JS))
        expect(trigger).to_have_count(1)
        expect(page.locator(".shvya-mobile-sidebar-backdrop")).to_have_count(1)
        for _ in range(2):
            trigger.click()
            if method == "backdrop":
                backdrop = page.locator(".shvya-mobile-sidebar-backdrop")
                assert not backdrop.evaluate("node=>!!node.closest('[inert]')")
                page.touchscreen.tap(385, 200)
            elif method == "escape":
                page.keyboard.press("Escape")
            elif method == "history":
                page.evaluate("document.dispatchEvent(new Event('htmx:beforeHistorySave'))")
            else:
                page.set_viewport_size({"width": 1280, "height": 800})
            expect(trigger).to_have_attribute("aria-expanded", "false")
            expect(page.locator("#workspace")).not_to_have_attribute("inert", "")
            assert page.locator("main").evaluate("node=>getComputedStyle(node).overflowY") == "auto"
            expect(page.locator("#already-inert")).to_have_attribute("inert", "")
            if method == "resize":
                page.set_viewport_size({"width": 390, "height": 844})
            hit_target(page.locator("#content-action"))
            page.locator("#content-action").click()
    finally:
        context.close()


@pytest.mark.parametrize("crm", [False, True], ids=["standard", "crm"])
@pytest.mark.parametrize("collapsed", [False, True])
def test_desktop_sidebar_still_clickable(browser, crm, collapsed):
    context, page = mount(browser, crm=crm, collapsed=collapsed, size=(1280, 800))
    try:
        expect(page.locator(".shvya-mobile-sidebar-backdrop")).not_to_be_visible()
        direct = page.get_by_role("link", name="Sales Desk", exact=True)
        hit_target(direct)
        direct.click()
        expect(page).to_have_url(re.compile(r"#sales-desk$"))
        toggle = page.locator("[data-shvya-sidebar-toggle]")
        hit_target(toggle)
        toggle.click()
        assert page.evaluate("window.desktopToggleCalls") == 1
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == str(int(not collapsed))
    finally:
        context.close()
