"""Exercise the real consent UI and script without contacting Google."""
import os
from pathlib import Path

from django.template.loader import render_to_string
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def test_consent_controls_network_and_removes_actual_browser_cookies():
    html = render_to_string('marketing/signup_complete.html', {
        'marketing_ga_id': 'G-TEST123', 'signup_receipt': 'private-receipt',
    })
    google_requests = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('SHVYA_TEST_BROWSER'))
        context = browser.new_context()
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def route(r):
            url = r.request.url
            if 'googletagmanager.com' in url:
                google_requests.append(url)
                r.fulfill(content_type='application/javascript', body='/* mocked Google boundary */')
            elif '/static/marketing/analytics.js' in url:
                r.fulfill(path=str(ROOT / 'static/marketing/analytics.js'))
            elif '/static/marketing/analytics.css' in url:
                r.fulfill(path=str(ROOT / 'static/marketing/analytics.css'))
            elif '/static/' in url:
                r.fulfill(body='')
            else:
                r.fulfill(content_type='text/html', body=html)

        context.route('**/*', route)
        page.goto('https://shvya-ai.com/signup/complete/?email=private@example.com#token')
        expect(page.get_by_role('button', name='Allow analytics', exact=True)).to_be_visible()
        assert not google_requests
        page.get_by_role('button', name='Decline', exact=True).click()
        assert not google_requests
        page.get_by_role('button', name='Analytics preferences', exact=True).click()
        page.get_by_role('button', name='Allow analytics', exact=True).click()
        page.wait_for_function('window.dataLayer.some(a => a[0] === "event" && a[1] === "sign_up")')
        assert len(google_requests) == 1
        payload = page.evaluate('JSON.stringify(window.dataLayer)')
        assert 'private' not in payload
        assert '#token' not in payload
        context.add_cookies([
            {'name': '_ga', 'value': 'test', 'domain': '.shvya-ai.com', 'path': '/'},
            {'name': '_ga_TEST123', 'value': 'test', 'domain': '.shvya-ai.com', 'path': '/'},
        ])
        page.get_by_role('button', name='Analytics preferences', exact=True).click()
        page.get_by_role('button', name='Decline', exact=True).click()
        assert not [c for c in context.cookies() if c['name'].startswith('_ga')]
        assert page.evaluate('window["ga-disable-G-TEST123"]')
        page.reload()
        assert len(google_requests) == 1
        assert not errors
        browser.close()
