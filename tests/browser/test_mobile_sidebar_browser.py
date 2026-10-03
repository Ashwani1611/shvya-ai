"""Browser contracts for mobile shell geometry and controller ownership.

Uses a permission-neutral shell fixture, not a logged-in production session.
Run: pytest tests/browser/test_mobile_sidebar_browser.py -q
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE when using a system Chromium binary.
"""
from pathlib import Path
import os

import pytest
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
# Model legacy utility layout and the competing desktop rules explicitly. This
# is deliberately a DOM contract fixture; authenticated page coverage is separate.
BASE_CSS = """
*{box-sizing:border-box}body{margin:0;font:14px system-ui;color:#1d1d1f;background:#f5f5f7}
button,a,input{font:inherit}button,a{cursor:pointer}a{color:inherit;text-decoration:none}
html,body{height:100%;overflow:hidden}.app{display:flex;height:100dvh;overflow:hidden}
#app-sidebar{width:260px;flex:0 0 260px;height:calc(100vh - 24px);margin:12px;border-radius:22px;background:#fff;overflow:hidden}
#app-sidebar.sidebar-collapsed{width:72px!important;flex-basis:72px!important}
#app-sidebar+div{display:flex;flex-direction:column;flex:1;min-width:0;overflow:hidden}
#app-sidebar+div>header{display:flex;align-items:center;justify-content:space-between;margin:12px;padding:10px 12px 10px 20px!important;border-radius:22px;background:#fff;flex:none}
#app-sidebar+div>header>h1{font-size:18px;white-space:nowrap}
#app-sidebar+div>header>div{display:flex;gap:7px}header button,header a{border:1px solid #ddd;border-radius:14px;padding:8px;display:flex;align-items:center;gap:8px}
#app-sidebar+div>main{flex:1;min-height:0;overflow-y:auto!important;padding:24px}.card{padding:20px;background:white;border-radius:16px;margin-bottom:12px}
.shvya-sidebar-shell{display:flex;flex-direction:column;height:100%;padding:12px}
.shvya-sidebar-brand{display:flex;align-items:center;height:44px;flex:none;gap:10px}.shvya-brand-name{flex:1}
.shvya-sidebar-toggle{width:30px;height:30px;border:1px solid #ddd;background:white}
.shvya-search-trigger{display:flex;align-items:center;width:100%;height:42px;margin:10px 0 12px;border:1px solid #ddd;border-radius:20px;background:white}
.shvya-search-copy{flex:1}.shvya-sidebar-scroll{flex:1;min-height:0;overflow:auto}
.shvya-nav-stack,.shvya-nav-children{display:flex;flex-direction:column;gap:3px}.shvya-nav-children{min-height:0;overflow:hidden;padding-left:12px}
.shvya-nav-row{display:flex;align-items:center;gap:10px;height:42px;min-height:42px;width:100%;padding:0 11px;border:0;background:none;border-radius:12px;text-align:left}
.shvya-nav-icon{width:20px;min-width:20px}.shvya-nav-copy{flex:1;min-width:0;white-space:nowrap;overflow:hidden}.shvya-nav-row.is-active{background:#edf4ff}
.shvya-nav-children-grid{display:grid;grid-template-rows:0fr;opacity:0}.shvya-nav-group.is-open>.shvya-nav-children-grid{grid-template-rows:1fr;opacity:1}
.shvya-sidebar-footer{flex:none;border-top:1px solid #ddd;padding-top:10px}.shvya-profile-card{height:48px;display:flex;align-items:center;gap:10px}
#app-sidebar.sidebar-collapsed .shvya-nav-row{width:42px!important;min-width:42px;max-width:42px;height:42px!important;padding:0!important;gap:0!important}
#app-sidebar.sidebar-collapsed .shvya-nav-copy,#app-sidebar.sidebar-collapsed .shvya-brand-name{opacity:0;pointer-events:none}
#app-sidebar.sidebar-collapsed .shvya-nav-group.is-open>.shvya-nav-children-grid{grid-template-rows:0fr;opacity:0}
.shvya-mobile-sidebar-trigger,.shvya-mobile-sidebar-backdrop{display:none}.shvya-command-layer{display:none;position:fixed;inset:20px;z-index:200;background:white;padding:20px}.shvya-command-layer.is-open{display:block}
@media(max-width:900px){.shvya-mobile-sidebar-trigger{display:inline-flex;align-items:center;justify-content:center;position:fixed;z-index:90;background:white;border:1px solid #ddd}.shvya-mobile-sidebar-backdrop{display:block;position:fixed;inset:0;border:0;opacity:0;pointer-events:none}html.shvya-mobile-sidebar-open .shvya-mobile-sidebar-backdrop{opacity:1;pointer-events:auto}html.shvya-mobile-sidebar-open .shvya-mobile-sidebar-trigger{opacity:0;pointer-events:none}}
@media(max-width:700px){header>div>a,header>div>button{width:38px;min-width:38px;padding:0!important}header>div>a>span,header>div>button>span{display:none}}
"""


def row(label: str, *, extra: str = "") -> str:
    return f'<a class="shvya-nav-row {extra}" href="#test"><i class="shvya-nav-icon" aria-hidden="true">◆</i><span class="shvya-nav-copy">{label}</span></a>'


def fixture_html(collapsed: bool) -> str:
    nav = "".join(row(label) for label in [
        "CRM", "Sales Desk", "SHVYA Calendar", "Call Intelligence", "SHVYA Sales",
        "Cadence", "AI Brain", "Workflows", "Insights", "Instagram", "Teams", "Integrations",
        "Very long navigation label that must remain readable on a narrow phone",
    ])
    return f'''<!doctype html><html class="shvya-premium-shell"><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>{BASE_CSS}</style></head>
<body><div class="app"><aside id="app-sidebar" class="shvya-premium-sidebar-ready {'sidebar-collapsed' if collapsed else ''}">
<div class="shvya-sidebar-shell"><div class="shvya-sidebar-brand"><span class="shvya-brand-name">SHVYA AI</span><button type="button" class="shvya-sidebar-toggle" data-shvya-sidebar-toggle aria-label="Collapse sidebar"><i class="ti ti-layout-sidebar-left-collapse"></i></button></div>
<button class="shvya-search-trigger"><span class="shvya-search-copy">Search or jump to...</span></button>
<div class="shvya-sidebar-scroll"><section class="shvya-nav-section"><div class="shvya-nav-stack">{nav}
<div class="shvya-nav-group"><button class="shvya-nav-row" aria-expanded="false"><i class="shvya-nav-icon" aria-hidden="true">◆</i><span class="shvya-nav-copy">WhatsApp</span></button><div class="shvya-nav-children-grid"><div class="shvya-nav-children">{row('API accounts')}{row('Hosted accounts')}</div></div></div></section></div>
<div class="shvya-sidebar-footer">{row('Help & Support')}{row('Settings')}{row('Logout')}<a class="shvya-profile-card" href="#profile"><span class="shvya-profile-name">Sample account</span></a></div></div></aside>
<div><header><h1>WhatsApp accounts and conversations</h1><div><span class="ai-credit-badge">5,000 credits</span>{''.join('<button><i>+</i><span>'+s+'</span></button>' for s in ['Reminders','Import','New lead','Profile','Logout'])}</div></header><main>{''.join('<section class="card"><h2>Workspace content</h2><input aria-label="Lead name" placeholder="Lead name"></section>' for _ in range(20))}</main></div></div>
<div id="already-inert" inert>Existing noninteractive content</div><div class="shvya-command-layer"><input aria-label="Search workspace"><button class="palette-close">Close search</button></div>
<script>
// An offline about:blank fixture has no storage origin. Model the preference
// store and competing desktop listeners without making network requests.
const preferences=new Map();
Object.defineProperty(window, 'localStorage', {{value: {{getItem: k => preferences.get(k) ?? null, setItem: (k,v) => preferences.set(k,String(v))}}, configurable: true}});
window.desktopGroupCalls=0;window.desktopToggleCalls=0;
// Let the sidebar see ordinary link clicks; suppress only the fixture navigation.
document.addEventListener('click',e => {{if(e.target.closest('a[href]'))e.preventDefault();}});
const sidebar=document.getElementById('app-sidebar');
localStorage.setItem('shvya-sidebar-collapsed', '{'1' if collapsed else '0'}');
document.querySelector('.shvya-nav-group>button').addEventListener('click',function(){{window.desktopGroupCalls++;sidebar.classList.remove('sidebar-collapsed');localStorage.setItem('shvya-sidebar-collapsed','0');}});
document.querySelector('[data-shvya-sidebar-toggle]').addEventListener('click',function(){{window.desktopToggleCalls++;sidebar.classList.toggle('sidebar-collapsed');localStorage.setItem('shvya-sidebar-collapsed',sidebar.classList.contains('sidebar-collapsed')?'1':'0');}});
const palette=document.querySelector('.shvya-command-layer');let returnFocus;
function openPalette(){{returnFocus=document.activeElement;palette.classList.add('is-open');palette.querySelector('input').focus();}}
function closePalette(){{palette.classList.remove('is-open');if(returnFocus)returnFocus.focus();}}
document.querySelector('.shvya-search-trigger').addEventListener('click',openPalette);
document.querySelector('.palette-close').addEventListener('click',closePalette);
document.addEventListener('keydown',function(e){{if((e.ctrlKey||e.metaKey)&&e.key==='k')openPalette();if(e.key==='Escape'&&palette.classList.contains('is-open'))closePalette();}});
</script></body></html>'''


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
        yield instance
        instance.close()


def mount(browser, width=390, height=844, collapsed=False):
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
    page.set_default_timeout(3000)
    page.set_content(fixture_html(collapsed))
    page.add_style_tag(path=str(ROOT / "static/css/shvya_premium_shell_mobile_patch.css"))
    page.add_script_tag(path=str(ROOT / "static/js/shvya_premium_sidebar_mobile.js"))
    return page


@pytest.mark.parametrize("width,height", [(320, 568), (360, 640), (390, 844), (768, 1024), (900, 650), (844, 390)])
@pytest.mark.parametrize("collapsed", [False, True])
def test_mobile_geometry_and_close(browser, width, height, collapsed):
    page = mount(browser, width, height, collapsed)
    try:
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        trigger = page.get_by_role("button", name="Open navigation", exact=True)
        assert trigger.bounding_box()["width"] >= 44
        assert trigger.bounding_box()["height"] >= 44
        trigger.click()
        expect(page.get_by_role("dialog", name="Main navigation")).to_be_visible()
        box = page.locator("#app-sidebar").bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= width
        assert box["width"] >= min(288, width - 32)
        assert box["y"] + box["height"] <= height
        assert page.locator("#app-sidebar .shvya-nav-row").first.bounding_box()["height"] >= 44
        assert page.locator("main").evaluate("e => getComputedStyle(e).overflowY") == "hidden"
        page.get_by_role("button", name="Close navigation", exact=True).first.click()
        expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_focused()
        assert page.locator("main").evaluate("e => getComputedStyle(e).overflowY") == "auto"
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == str(int(collapsed))
        assert page.evaluate("window.desktopToggleCalls") == 0
        expect(page.locator("#already-inert")).to_have_attribute("inert", "")
    finally:
        page.close()


def test_groups_focus_and_desktop_preference(browser):
    page = mount(browser, collapsed=True)
    try:
        page.get_by_role("button", name="Open navigation", exact=True).click()
        group = page.locator(".shvya-nav-group>button")
        group.click()
        expect(group).to_have_attribute("aria-expanded", "true")
        expect(page.get_by_role("link", name="Hosted accounts")).to_be_visible()
        assert page.evaluate("window.desktopGroupCalls") == 0
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == "1"
        group.click()
        expect(page.locator(".shvya-nav-children-grid")).to_have_attribute("inert", "")
        page.locator(".shvya-profile-card").focus()
        page.keyboard.press("Tab")
        expect(page.locator("[data-shvya-sidebar-toggle]")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator(".shvya-profile-card")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_focused()
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
    finally:
        page.close()


def test_resize_restores_desktop_and_reentry(browser):
    page = mount(browser, collapsed=True)
    try:
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.set_viewport_size({"width": 1280, "height": 800})
        page.wait_for_function("!document.documentElement.classList.contains('shvya-mobile-sidebar-open')")
        expect(page.locator("#app-sidebar")).not_to_have_attribute("inert", "")
        assert not page.evaluate("document.documentElement.classList.contains('shvya-mobile-sidebar-open')")
        assert page.locator("#app-sidebar").bounding_box()["width"] == 72
        assert page.evaluate("localStorage.getItem('shvya-sidebar-collapsed')") == "1"
        page.locator("[data-shvya-sidebar-toggle]").click()
        assert page.evaluate("window.desktopToggleCalls") == 1
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("shortcut", [False, True])
def test_search_handoff_and_backdrop(browser, shortcut):
    page = mount(browser)
    try:
        page.get_by_role("button", name="Open navigation", exact=True).click()
        if shortcut:
            page.keyboard.press("Control+k")
        else:
            page.locator(".shvya-search-trigger").click()
        expect(page.get_by_role("textbox", name="Search workspace")).to_be_focused()
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        page.keyboard.press("Escape")
        expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_focused()
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.locator(".shvya-mobile-sidebar-backdrop").click(position={"x": 385, "y": 200})
        expect(page.locator(".shvya-mobile-sidebar-trigger")).to_be_focused()
    finally:
        page.close()


def test_navigation_history_and_duplicate_initialization(browser):
    page = mount(browser)
    try:
        page.add_script_tag(path=str(ROOT / "static/js/shvya_premium_sidebar_mobile.js"))
        assert page.locator(".shvya-mobile-sidebar-trigger").count() == 1
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.get_by_role("link", name="CRM", exact=True).click()
        expect(page.locator("#app-sidebar")).to_have_attribute("inert", "")
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.evaluate("document.dispatchEvent(new Event('htmx:beforeHistorySave'))")
        assert not page.evaluate("document.documentElement.classList.contains('shvya-mobile-sidebar-open')")
        assert not page.locator("#app-sidebar+div").evaluate("e => e.inert")
    finally:
        page.close()
