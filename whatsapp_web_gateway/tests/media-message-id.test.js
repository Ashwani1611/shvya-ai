'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { patchMediaMessageId } = require('../scripts/media-message-id-patch');

const fixture = `const message = {
            id: newMsgKey,
            ...mediaOptions,
            ...botOptions,
            ...extraOptions,
        };
message;`;

test('outgoing media retains its message key and attachment metadata', () => {
  const context = { newMsgKey: { id: 'outgoing', fromMe: true },
    mediaOptions: { __x_id: undefined, mimetype: 'application/pdf', caption: 'Your quotation' },
    botOptions: {}, extraOptions: {} };
  assert.equal(Object.hasOwn(vm.runInNewContext(fixture, {...context}), '__x_id'), true);
  const result = vm.runInNewContext(patchMediaMessageId(fixture), {...context});
  assert.equal(Object.hasOwn(result, '__x_id'), false);
  assert.equal(result.id, context.newMsgKey);
  assert.equal(result.mimetype, 'application/pdf');
  assert.equal(result.caption, 'Your quotation');
});
test('patch is idempotent and fails closed on upstream drift', () => {
  const patched = patchMediaMessageId(fixture);
  assert.equal(patchMediaMessageId(patched), patched);
  assert.throws(() => patchMediaMessageId('new upstream shape'));
  assert.throws(() => patchMediaMessageId(fixture + fixture));
});
