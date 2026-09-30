"""Sandbox stage/file preview with all network traffic intercepted."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.template import Context, Engine
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def test_sandbox_stage_file_preview_and_restart():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed.")
            raise
        page = browser.new_page()
        try:
            stage = SimpleNamespace(id='stage-one', name='Qualified', is_active=True)
            pipeline = SimpleNamespace(name='Sales', stages=SimpleNamespace(all=[stage]))
            partial = (ROOT / 'templates/crm/knowledge_base/_ai_sandbox.html').read_text(encoding='utf-8')
            html = Engine().from_string(partial).render(Context({'sandbox_pipelines': [pipeline]}))
            html = '<div id="ai-setup-page"><input name="csrfmiddlewaretoken" value="test">' + html + '</div>'
            calls = []
            def intercept(route):
                if route.request.url.endswith('/playground/'):
                    calls.append((route.request.method, json.loads(route.request.post_data)))
                    if len(calls) == 1:
                        route.fulfill(status=400, json={
                            'error': 'Choose an active stage in your organization and restart the test.',
                            'stage': {'name': 'Rejected stage', 'pipeline': 'Sales'},
                            'events': [{'type': 'stage_transition', 'stage': 'Rejected stage', 'pipeline': 'Sales'}],
                            'files': [{'name': 'Rejected.pdf', 'url': '/api/v1/ai-engagement/playground/files/13/'}],
                        })
                        return
                    route.fulfill(json={'response': 'Here is the guide.', 'stage': {'name': 'Demo Requested', 'pipeline': 'Sales'},
                        'events': [{'type': 'stage_transition', 'stage': 'Demo Requested', 'pipeline': 'Sales'}],
                        'files': [{'name': 'Guide.xlsx', 'url': '/api/v1/ai-engagement/playground/files/12/'},
                                  {'name': 'Unsafe', 'url': 'https://other.test/private'}]})
                else:
                    route.fulfill(body=html, content_type='text/html')
            page.route('**/*', intercept)
            page.goto('http://sandbox.test/')
            page.add_script_tag(content=(ROOT / 'static/js/ai_setup_playground.js').read_text(encoding='utf-8'))
            page.locator('#playground-start-stage').select_option('stage-one')
            page.locator('#playground-message-input').fill('I want a demo')
            page.locator('#playground-send-message').click()
            expect(page.locator('#playground-messages')).to_contain_text('Choose an active stage')
            expect(page.locator('#playground-start-stage')).to_be_enabled()
            expect(page.locator('#playground-current-stage')).not_to_contain_text('Rejected stage')
            expect(page.get_by_role('link', name='Download Rejected.pdf')).to_have_count(0)
            page.locator('#playground-message-input').fill('I want a demo')
            page.locator('#playground-send-message').click()
            expect(page.locator('#playground-current-stage')).to_have_text('Sales / Demo Requested')
            expect(page.locator('#playground-start-stage')).to_be_disabled()
            expect(page.get_by_role('link', name='Download Guide.xlsx')).to_have_attribute('href', '/api/v1/ai-engagement/playground/files/12/')
            expect(page.get_by_role('link', name='Download Unsafe')).to_have_count(0)
            self_messages = page.locator('#playground-messages').inner_text()
            assert self_messages.index('Here is the guide.') < self_messages.index('Download Guide.xlsx')
            assert calls[0][1]['stage_id'] == 'stage-one'
            page.locator('#playground-restart-chat').click()
            expect(page.locator('#playground-start-stage')).to_be_enabled()
            expect(page.locator('#playground-messages')).to_be_empty()
            assert calls[-1][0] == 'DELETE'
        finally:
            browser.close()
