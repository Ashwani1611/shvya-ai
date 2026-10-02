/* Hosted inbox state reconciliation. No network or DOM side effects. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ShvyaHostedLiveState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function order(a, b) {
    return String(a.created_at || '').localeCompare(String(b.created_at || '')) ||
      String(a.id || '').localeCompare(String(b.id || ''));
  }
  function create() {
    let revision = 0;
    const versions = new Map();
    const stamps = new Map();
    function reset() { versions.clear(); stamps.clear(); revision += 1; }
    function apply(messages, event) {
      const message = event.message;
      const id = String(event.message_id || message?.id || '');
      if (!id) return false;
      const stamp = String(event.updated_at || message?.updated_at || '');
      if (stamp && stamps.has(id) && stamp <= stamps.get(id)) return false;
      if (stamp) stamps.set(id, stamp);
      versions.set(id, ++revision);
      if (event.operation === 'remove') messages.delete(id);
      else if (message) messages.set(id, message);
      else return false;
      return true;
    }
    function merge(messages, data, startedAt, older) {
      const incoming = data.thread || [];
      const ids = new Set(incoming.map(message => String(message.id)));
      const first = incoming.reduce((oldest, message) => !oldest || order(message, oldest) < 0 ? message : oldest, null);
      // The latest page is authoritative within its range. This removes a
      // cancelled draft without throwing away pages explicitly loaded earlier.
      if (!older) {
        for (const [id, message] of messages) {
          if ((versions.get(id) || 0) > startedAt || ids.has(id)) continue;
          if (!data.has_more || (first && order(message, first) >= 0)) messages.delete(id);
        }
      }
      for (const message of incoming) {
        const id = String(message.id || '');
        if (!id || (versions.get(id) || 0) > startedAt) continue;
        messages.set(id, message);
      }
    }
    return { apply, merge, reset, revision: () => revision };
  }
  function matches(selected, event) {
    return Boolean(selected && [event.chat_key, ...(event.aliases || [])].includes(selected));
  }
  return { create, matches, order };
});
