'use strict';

// https://github.com/wwebjs/whatsapp-web.js/issues/201922
// MediaData.__x_id otherwise overwrites the outgoing Msg's valid identity.
function patchMediaMessageId(source) {
  const before = '            ...botOptions,\n            ...extraOptions,\n        };';
  const after = before + '\n        delete message.__x_id;';
  if (source.includes(after)) return source;
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
    throw new Error('Cannot uniquely locate outgoing media message construction.');
  }
  return source.replace(before, after);
}

module.exports = { patchMediaMessageId };
