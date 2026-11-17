const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/live.js"), "utf8");

function events(target = {}) {
  const handlers = {};
  target.addEventListener = (name, handler) => {
    (handlers[name] ||= []).push(handler);
  };
  target.dispatch = (name, event = {}) => {
    for (const handler of handlers[name] || []) handler(event);
  };
  return target;
}

function browser() {
  const message = { textContent: "" };
  const button = events({ hidden: true });
  const live = {
    hidden: true,
    dataset: { url: "/live", revision: "0", changed: "New changes", offline: "Offline",
      session: "Session expired", confirm: "Discard edits?" },
    querySelector: (selector) => selector === "button" ? button : message,
    contains: (element) => element === button
  };
  const document = events({
    hidden: false, activeElement: null,
    getElementById: () => live
  });
  let reloads = 0;
  const window = events({
    location: { reload: () => { reloads += 1; } },
    confirm: () => false
  });
  const timers = new Map();
  let timerId = 0;
  let calls = 0;
  let reply = async () => ({ ok: true, status: 200, redirected: false,
    headers: new Map([["content-type", "application/json"]]), json: async () => ({ revision: "0" }) });
  vm.runInNewContext(source, {
    document, window, AbortController,
    fetch: (...args) => { calls += 1; return reply(...args); },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id)
  });
  return {
    document, window, live, button, message,
    get reloads() { return reloads; },
    get calls() { return calls; },
    respond: (revision) => {
      reply = async () => ({ ok: true, status: 200, redirected: false,
        headers: new Map([["content-type", "application/json"]]), json: async () => ({ revision }) });
    },
    fail: () => { reply = async () => { throw new Error("Network unavailable"); }; },
    expire: () => { reply = async () => ({ redirected: true, status: 200 }); },
    tick: async () => {
      const [id, timer] = [...timers.entries()].find(([, timer]) => timer.delay <= 5000);
      timers.delete(id);
      await timer.callback();
    }
  };
}

test("idle pages refresh when a team change arrives", async () => {
  const app = browser();
  app.respond("1");
  await app.tick();
  assert.equal(app.reloads, 1);
});

test("unfinished forms survive changes and require confirmation before reloading", async () => {
  const app = browser();
  app.document.dispatch("input", { target: {} });
  app.respond("1");
  await app.tick();
  await app.tick();
  assert.equal(app.reloads, 0);
  assert.equal(app.message.textContent, "New changes");
  assert.equal(app.live.hidden, false);
  assert.equal(app.button.hidden, false);
  app.button.dispatch("click");
  assert.equal(app.reloads, 0);
  app.window.confirm = () => true;
  app.button.dispatch("click");
  assert.equal(app.reloads, 1);
});

test("focused controls and form submission are not interrupted", async () => {
  const app = browser();
  app.document.activeElement = { matches: () => true };
  app.respond("1");
  await app.tick();
  assert.equal(app.reloads, 0);
  app.document.activeElement = null;
  await app.tick();
  assert.equal(app.reloads, 1);
  app.document.dispatch("submit");
  const calls = app.calls;
  await app.tick();
  assert.equal(app.calls, calls);
});

test("connectivity failures are visible and recover without losing pending changes", async () => {
  const app = browser();
  app.document.dispatch("change", { target: {} });
  app.respond("1");
  await app.tick();
  app.fail();
  await app.tick();
  assert.equal(app.message.textContent, "Offline");
  app.respond("1");
  await app.tick();
  assert.equal(app.message.textContent, "New changes");
  assert.equal(app.reloads, 0);
});

test("session expiry is shown without discarding edits or following login redirects", async () => {
  const app = browser();
  app.expire();
  await app.tick();
  assert.equal(app.message.textContent, "Session expired");
  assert.equal(app.button.hidden, false);
  assert.equal(app.reloads, 0);
});

test("hidden pages pause polling until they become visible", async () => {
  const app = browser();
  app.document.hidden = true;
  await app.tick();
  assert.equal(app.calls, 0);
  app.document.hidden = false;
  app.document.dispatch("visibilitychange");
  await app.tick();
  assert.equal(app.calls, 1);
});
