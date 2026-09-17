"""Presentation contracts for the Workflows page; no provider/database calls."""

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "templates/triggers/dashboard.html"
CSS = ROOT / "static/triggers/apple_workflows.css"
JS = ROOT / "static/triggers/apple_workflows.js"


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class WorkflowsPresentationTests(TestCase):
    def setUp(self):
        self.html = TEMPLATE.read_text(encoding="utf-8")
        self.css = CSS.read_text(encoding="utf-8")
        self.js = JS.read_text(encoding="utf-8")
        self.elements = Elements(self.html).elements
        self.ids = {attrs["id"]: attrs for _, attrs in self.elements if "id" in attrs}

    def test_unique_ids_and_existing_controller_hooks(self):
        counts = Counter(attrs["id"] for _, attrs in self.elements if "id" in attrs)
        self.assertTrue(all(count == 1 for count in counts.values()))
        controller = (ROOT / "static/triggers/dashboard.js").read_text(encoding="utf-8")
        hooks = set(re.findall(r"\$\(['\"](st-[\w-]+)['\"]\)", controller))
        self.assertGreater(len(hooks), 30)
        self.assertEqual(hooks - self.ids.keys(), set())

    def test_keeps_permission_and_csrf_boundaries(self):
        self.assertIn("{% csrf_token %}", self.html)
        self.assertIn("{% url 'crm-smart-triggers' %}", self.html)
        self.assertIn("{{ trigger_admin|yesno:'true,false' }}", self.html)
        admin_blocks = re.findall(
            r"{% if trigger_admin %}(.*?){% endif %}", self.html, re.S
        )
        for control in ("st-new", "st-save"):
            self.assertTrue(any(f'id="{control}"' in block for block in admin_blocks))
        self.assertIn('trigger_catalog|json_script:"trigger-catalog"', self.html)

    def test_accessible_tabs_have_linked_panels(self):
        for view in ("rules", "history"):
            tab = self.ids[f"st-{view}-tab"]
            panel = self.ids[f"st-{view}-panel"]
            self.assertEqual(tab["role"], "tab")
            self.assertEqual(tab["aria-controls"], f"st-{view}-panel")
            self.assertEqual(panel["role"], "tabpanel")
            self.assertEqual(panel["aria-labelledby"], f"st-{view}-tab")
        self.assertEqual(self.ids["st-history-tab"]["tabindex"], "-1")
        self.assertIn("hidden", self.ids["st-history-panel"])
        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(f"'{key}'", self.js)
        self.assertIn("tabs[next].click()", self.js)
        self.assertNotIn("fetch(", self.js)

    def test_labels_descriptions_and_dialog_are_connected(self):
        for _, attrs in self.elements:
            for attribute in ("aria-labelledby", "aria-describedby", "aria-controls"):
                for target in attrs.get(attribute, "").split():
                    self.assertIn(target, self.ids)
        self.assertIn("autofocus", self.ids["st-delete-cancel"])
        self.assertEqual(self.ids["st-form-error"]["role"], "alert")
        self.assertEqual(self.ids["st-notice"]["aria-live"], "polite")
        self.assertFalse(any(tag == "main" for tag, _ in self.elements))

    def test_metrics_are_not_mocked_in_the_production_template(self):
        for metric in ("st-total", "st-active"):
            self.assertIn(f'<strong id="{metric}">—</strong>', self.html)
        self.assertIn('id="st-rule-list"', self.html)
        self.assertIn("Loading workflows…", self.html)

    def test_every_css_rule_is_scoped_to_workflows(self):
        css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        selectors = re.findall(r"(?:^|[{}])\s*([^{}]+)\{", css)
        self.assertGreater(len(selectors), 80)
        for selector in selectors:
            if not selector.lstrip().startswith("@"):
                self.assertTrue(selector.strip().startswith("html.shvya-page-workflows"), selector)
        self.assertNotIn(":root", css)
        self.assertNotIn("@import", css)

    def test_accessibility_and_cache_invalidation_are_explicit(self):
        for setting in ("prefers-reduced-motion", "prefers-contrast", "forced-colors"):
            self.assertIn(setting, self.css)
        self.assertIn(":focus-visible", self.css)
        for asset in ("apple_workflows.css", "apple_workflows.js"):
            self.assertIn("triggers/" + asset + "' %}?v=20260918-2", self.html)
