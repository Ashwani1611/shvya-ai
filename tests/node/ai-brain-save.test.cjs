const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../../static/js/ai_brain.js'), 'utf8');

function harness(reply) {
    const node = () => ({ dataset: {}, value: '', textContent: '', addEventListener() {}, setAttribute() {}, querySelectorAll() { return []; } });
    const playbook = { ...node(), value: '## Rules\nBe helpful.\n## Welcome Message\nHello!' };
    const status = node();
    const button = { disabled: false };
    let submit;
    const form = { action: 'https://example.test/ai-setup/', querySelector: () => button,
        addEventListener: (event, handler) => { if (event === 'submit') submit = handler; } };
    const selected = { '#ai_playbook': playbook, '#org-ai-settings-form': form, '[data-save-status]': status };
    const page = { dataset: {}, querySelector: selector => selected[selector] || node(), querySelectorAll: () => [] };
    const redirects = [];
    class FormData {
        constructor() { this.playbook = playbook.value; }
        get(name) { return name === 'ai_playbook' ? this.playbook : null; }
    }
    vm.runInNewContext(script, {
        document: { readyState: 'complete', querySelector: () => page, addEventListener() {} },
        window: { location: { assign: url => redirects.push(url) } },
        FormData,
        fetch: async (url, options) => {
            assert.equal(url, form.action);
            assert.equal(options.method, 'POST');
            assert.equal(options.headers.Accept, 'application/json');
            assert.equal(options.credentials, 'same-origin');
            return reply({ playbook, payload: options.body });
        },
    });
    return { playbook, status, button, redirects, submit: () => submit({ preventDefault() {} }) };
}

test('a persisted multiline Playbook confirms despite multipart CRLF normalization', async () => {
    const h = harness(({ payload }) => ({ ok: true, json: async () => ({ saved: true, ai_playbook: payload.get('ai_playbook').replace(/\n/g, '\r\n') }) }));
    await h.submit();
    assert.equal(h.status.textContent, 'Saved successfully.');
    assert.deepEqual(h.redirects, ['https://example.test/ai-setup/']);
    assert.equal(h.button.disabled, false);
});

test('a rejected save retains the draft and shows the backend error', async () => {
    const h = harness(() => ({ ok: false, json: async () => ({ saved: false, error: 'Invalid website URL' }) }));
    const draft = h.playbook.value;
    await h.submit();
    assert.equal(h.playbook.value, draft);
    assert.equal(h.status.textContent, 'Invalid website URL');
    assert.equal(h.redirects.length, 0);
    assert.equal(h.button.disabled, false);
});

test('edits made while saving are preserved without reloading', async () => {
    const h = harness(({ playbook, payload }) => {
        playbook.value += '\nA newer rule.';
        return { ok: true, json: async () => ({ saved: true, ai_playbook: payload.get('ai_playbook') }) };
    });
    await h.submit();
    assert.match(h.playbook.value, /A newer rule/);
    assert.match(h.status.textContent, /newer unsaved edits/);
    assert.equal(h.redirects.length, 0);
});

test('a mismatching database readback is never reported as a confirmed save', async () => {
    const h = harness(() => ({ ok: true, json: async () => ({ saved: true, ai_playbook: 'An older version' }) }));
    await h.submit();
    assert.match(h.status.textContent, /differs from your draft/);
    assert.equal(h.redirects.length, 0);
});
