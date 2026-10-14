"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../../../static/superadmin/api-operations.js"), "utf8");

function target(values = {}) {
  const listeners = new Map();
  return Object.assign({
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(fn);
    },
    fire(name, event = {}) {
      for (const fn of listeners.get(name) || []) fn({ target: this, defaultPrevented: false, ...event });
    },
    matches() { return false; },
  }, values);
}
function input(values = {}) {
  return target({ name: "q", type: "search", value: "", defaultValue: "", ...values });
}
function setup({ preference = null, noToggle = false, hidden = false, online = true,
  storageError = false, fields = [input()], confirm = false } = {}) {
  const timers = new Map();
  let clock = 0, nextId = 0, reloads = 0, confirms = 0;
  const refresh = target();
  const toggle = noToggle ? null : target({ checked: false });
  const status = { textContent: "" }, controls = { hidden: true };
  const form = target({ elements: fields, closest() { return this; } });
  const document = target({ hidden, activeElement: target(),
    querySelector(selector) {
      return { "[data-api-refresh]": refresh, "[data-api-auto-refresh]": toggle,
        "[data-api-refresh-controls]": controls, "[data-api-refresh-status]": status }[selector] || null;
    },
    querySelectorAll() { return [form]; },
  });
  const saved = new Map(preference ? [["shvya-api-operations-auto-refresh", preference]] : []);
  const location = { href: "https://example.test/superadmin/api/?range=90d&organization=unattributed&q=429&page=2",
    reload() { reloads++; } };
  const window = target({ document, navigator: { onLine: online }, location,
    sessionStorage: {
      getItem(key) { if (storageError) throw Error("storage blocked"); return saved.get(key) || null; },
      setItem(key, value) { if (storageError) throw Error("storage blocked"); saved.set(key, value); },
    },
    confirm() { confirms++; return confirm; },
    setTimeout(fn, ms) { const id = ++nextId; timers.set(id, { fn, at: clock + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInNewContext(source, { document, window });
  return { document, window, form, fields, toggle, refresh, controls, status, saved, timers,
    get reloads() { return reloads; }, get confirms() { return confirms; },
    tick(ms) {
      const end = clock + ms;
      let runs = 0;
      while (true) {
        const item = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
        if (!item || item[1].at > end) break;
        assert.ok(++runs < 1000, "timer loop must be bounded");
        clock = item[1].at; timers.delete(item[0]); item[1].fn();
      }
      clock = end;
    },
    enable() { toggle.checked = true; toggle.fire("change"); },
  };
}

test("off by default with visible status and no timer", () => {
  const x = setup(); x.tick(120000);
  assert.equal(x.reloads, 0); assert.equal(x.timers.size, 0);
  assert.equal(x.controls.hidden, false); assert.match(x.status.textContent, /off/);
});
test("explicit opt-in reloads once after 60s and preserves the full URL", () => {
  const x = setup(); const url = x.window.location.href; x.enable();
  x.tick(59999); assert.equal(x.reloads, 0); x.tick(1); assert.equal(x.reloads, 1);
  assert.equal(x.window.location.href, url); x.tick(120000); assert.equal(x.reloads, 1);
});
test("session preference restores opt-in but not arbitrary values", () => {
  const x = setup({ preference: "on" }); x.tick(60000); assert.equal(x.reloads, 1);
  const y = setup({ preference: "yes" }); y.tick(60000); assert.equal(y.reloads, 0);
});
test("repeated toggle changes leave one timer; off cancels it", () => {
  const x = setup(); x.enable(); x.enable(); assert.equal(x.timers.size, 1);
  assert.equal(x.saved.get("shvya-api-operations-auto-refresh"), "on");
  x.toggle.checked = false; x.toggle.fire("change"); x.tick(120000);
  assert.equal(x.reloads, 0); assert.equal(x.saved.get("shvya-api-operations-auto-refresh"), "off");
});
test("hidden tabs cancel timers; return starts a fresh interval", () => {
  const x = setup({ preference: "on" }); x.tick(30000);
  x.document.hidden = true; x.document.fire("visibilitychange");
  assert.equal(x.timers.size, 0); x.tick(120000); assert.equal(x.reloads, 0);
  x.document.hidden = false; x.document.fire("visibilitychange");
  x.tick(59999); assert.equal(x.reloads, 0); x.tick(1); assert.equal(x.reloads, 1);
});
test("offline mode suspends refresh and resumes after reconnect", () => {
  const x = setup({ preference: "on", online: false }); assert.equal(x.timers.size, 0);
  assert.match(x.status.textContent, /offline/); x.tick(120000); assert.equal(x.reloads, 0);
  x.window.navigator.onLine = true; x.window.fire("online"); x.tick(60000); assert.equal(x.reloads, 1);
});
test("focused inputs pause; blurred unapplied edits remain protected", () => {
  const x = setup({ preference: "on" });
  x.document.activeElement = target({ matches() { return true; } }); x.document.fire("focusin");
  x.tick(60000); assert.equal(x.reloads, 0);
  x.fields[0].value = "429"; x.document.fire("input");
  x.document.activeElement = target(); x.document.fire("focusout"); x.tick(120000);
  assert.equal(x.reloads, 0); assert.match(x.status.textContent, /apply or reset/);
});
test("the auto-refresh checkbox retaining focus does not block refresh", () => {
  const x = setup(); x.document.activeElement = x.toggle; x.enable(); x.tick(60000);
  assert.equal(x.reloads, 1);
});
test("restored edits present before script execution are not treated as saved", () => {
  const x = setup({ preference: "on", fields: [input({ value: "restored", defaultValue: "" })] });
  x.tick(120000); assert.equal(x.reloads, 0);
});
test("a script-only field change is rechecked before reloading", () => {
  const x = setup({ preference: "on" }); x.fields[0].value = "unsaved";
  x.tick(60000); assert.equal(x.reloads, 0);
  x.fields[0].value = ""; x.tick(60000); assert.equal(x.reloads, 1);
});
test("reset event is processed after native form defaults are restored", () => {
  const x = setup({ preference: "on", fields: [input({ value: "429" })] });
  x.document.fire("reset"); x.fields[0].value = ""; x.tick(0); x.tick(60000);
  assert.equal(x.reloads, 1);
});
test("changed selects are protected even after blur", () => {
  const field = input({ type: "select-one", options: [
    { defaultSelected: true, selected: false }, { defaultSelected: false, selected: true },
  ] });
  const x = setup({ preference: "on", fields: [field] }); x.tick(60000); assert.equal(x.reloads, 0);
});
test("single select without selected attribute recognizes its default option", () => {
  const field = input({ type: "select-one", options: [
    { disabled: true, selected: false }, { defaultSelected: false, selected: true },
  ] });
  const x = setup({ preference: "on", fields: [field] }); x.tick(60000); assert.equal(x.reloads, 1);
});
test("checkboxes, radio buttons, multi-selects and files are protected", () => {
  for (const field of [input({ type: "checkbox", checked: true, defaultChecked: false }),
    input({ type: "radio", checked: false, defaultChecked: true }),
    input({ type: "select-multiple", multiple: true, options: [{ selected: true, defaultSelected: false }] }),
    input({ type: "file", files: [{}] })]) {
    const x = setup({ preference: "on", fields: [field] }); x.tick(60000); assert.equal(x.reloads, 0);
  }
});
test("disabled, hidden and unnamed fields do not pause the dashboard", () => {
  const x = setup({ preference: "on", fields: [input({ disabled: true, value: "x" }),
    input({ type: "hidden", value: "x" }), input({ name: "", value: "x" })] });
  x.tick(60000); assert.equal(x.reloads, 1);
});
test("contenteditable descendants pause refresh", () => {
  const x = setup({ preference: "on" });
  x.document.activeElement = target({ isContentEditable: true }); x.tick(60000); assert.equal(x.reloads, 0);
});
test("manual refresh protects edits when discard is cancelled", () => {
  const x = setup({ fields: [input({ value: "429" })] }); x.refresh.fire("click");
  assert.equal(x.confirms, 1); assert.equal(x.reloads, 0); assert.equal(x.fields[0].value, "429");
});
test("explicit discard permits a manual refresh", () => {
  const x = setup({ fields: [input({ value: "429" })], confirm: true }); x.refresh.fire("click");
  assert.equal(x.confirms, 1); assert.equal(x.reloads, 1);
});
test("detail pages never auto-refresh; clean manual refresh still works", () => {
  const x = setup({ noToggle: true, preference: "on" }); x.tick(120000);
  assert.equal(x.reloads, 0); assert.equal(x.timers.size, 0);
  x.refresh.fire("click"); assert.equal(x.reloads, 1); assert.equal(x.confirms, 0);
});
test("storage denial is nonfatal; explicit toggle still works", () => {
  const x = setup({ storageError: true, preference: "on" }); assert.equal(x.toggle.checked, false);
  x.enable(); x.tick(60000); assert.equal(x.reloads, 1);
});
test("leaving cancels pending work and Back restores one timer", () => {
  const x = setup({ preference: "on" }); x.window.fire("pagehide");
  x.tick(120000); assert.equal(x.reloads, 0); assert.equal(x.timers.size, 0);
  x.window.fire("pageshow", { persisted: true }); assert.equal(x.timers.size, 1);
  x.tick(60000); assert.equal(x.reloads, 1);
});
test("form submission suspends refresh while navigation is pending", () => {
  const x = setup({ preference: "on" }); x.document.fire("submit", { target: x.form });
  x.tick(120000); assert.equal(x.reloads, 0);
});
test("cancelled form submission leaves the refresh scheduler operational", () => {
  const x = setup({ preference: "on" }); x.document.fire("submit", { target: x.form, defaultPrevented: true });
  x.tick(60000); assert.equal(x.reloads, 1);
});
