# Mobile dashboard navigation

## Scope

The shared CRM dashboard shell keeps the existing Django templates, server-filtered
navigation, desktop sidebar, and command palette. No navigation entitlement,
organization permission, API, messaging, AI, infrastructure or deployment behavior
is changed. This change does not redesign every feature-specific screen or the
separate marketing, Django admin and superadmin shells.

## Behavior

At viewport widths of 900px or less:

- The menu button occupies its own header cell and renders its own SVG, independent
  of icon-font loading. Header actions keep their text in a locally scrollable row
  instead of widening the page or overlapping the title.
- The sidebar is a full-width drawer even when the desktop compact preference is
  saved. Mobile group clicks and close clicks do not invoke desktop preference writes.
- Menu/close targets are at least 44px; navigation rows are at least 46px. Long labels
  can wrap. The drawer observes dynamic viewport height and device safe-area insets.
- The central navigation scrolls independently; short landscape viewports allow the
  complete drawer to scroll so footer links remain reachable.
- Closed navigation is inert. Open navigation has dialog semantics, traps focus,
  blocks background interaction and main scrolling, and restores focus on closing.
- Close control, backdrop, Escape, normal navigation, desktop breakpoint changes,
  browser page restoration and HTMX history snapshots release drawer state.
- Search and Ctrl/Command+K close the drawer before handing off to the existing
  command palette, preventing the drawer from trapping the search input.

The existing mobile companion remains in place. The final mobile patch now loads
at the end of `templates/base/premium_shell_assets.html`, after shared desktop and
workspace styles. Changed mobile CSS/JS references have versioned URLs.

## Regression checks

`tests/browser/test_mobile_sidebar_browser.py` contains 17 Chromium checks using an
offline shared-shell DOM fixture and mocked desktop listeners/preference storage.
It covers 320, 360, 390, 768 and 900px widths, an 844x390 landscape viewport, both
saved compact states, 1280px desktop restoration, focus wrapping, submenu toggling,
search handoff, backdrop close, normal navigation, HTMX history and idempotent setup.

Run:

```sh
pytest tests/browser/test_mobile_sidebar_browser.py tests/browser/test_mobile_sidebar_capture_browser.py -q
# A system Chromium binary may be selected explicitly:
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium pytest tests/browser/test_mobile_sidebar_capture_browser.py -q
```

The fixture checks are not authenticated production testing, physical-device
validation, WebKit/Safari validation or a full Django test-suite run. Before release,
verify the real CRM, WhatsApp, AI Brain, Calendar and Sales pages on mobile, including
long organization names, plan restrictions, overlays and each page's own CSS.

## Search capture-order regression

`shvya_command_center.js` registers document-capture listeners and stops immediate
propagation for Search and Ctrl/Command+K. Sidebar-capture and later document-capture
listeners therefore cannot release an open mobile drawer before the center opens.
The mobile controller handles the handoff at window capture, then allows the
original event to continue to the command center. It does not open another palette,
write the compact preference, or change the command center's desktop handler.

`tests/browser/test_mobile_sidebar_capture_browser.py` adds eight checks using the
production command center's extracted capture-listener code with a stub palette UI.
Six mobile cases cover click, Control+K and Meta+K in both compact preference states;
two desktop cases protect existing behavior. The mobile cases run two open/search/
close cycles, verify typing/focus restoration, and preserve other modules' inert state.
The extraction fails explicitly when the production registration structure changes.
This is listener integration coverage, not full-command-center or page rendering.

On the initial PR head `3a75dc7`, the new capture-order checks reproduced six mobile
failures with two desktop passes. With window-capture handoff applied, all eight
passed locally; JavaScript syntax validation passed. The earlier `17 passed in 6.45s`
report records only the original fixture run, not a deployment or all-screen audit.
CI and Security also passed for that earlier head; newer commits require their own
CI results. The separate targeted-browser job being skipped on a full-suite CI run
is expected and is not by itself a missing-test failure.
