const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("password visibility toggles without a phone input", () => {
  const password = { type: "password", value: "secret" };
  const button = {
    hidden: true,
    dataset: { show: "Show password", hide: "Hide password" },
    attributes: { "aria-controls": "password", "aria-pressed": "false" },
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { if (name === "click") this.click = callback; }
  };
  const document = {
    querySelectorAll: (selector) => selector === "[data-password-toggle]" ? [button] : [],
    getElementById: (id) => id === "password" ? password : null
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/forms.js"), "utf8"), { document });
  assert.equal(button.hidden, false);
  button.click();
  assert.equal(password.type, "text");
  assert.equal(password.value, "secret");
  assert.equal(button.textContent, "Hide password");
  assert.equal(button.attributes["aria-pressed"], "true");
  button.click();
  assert.equal(password.type, "password");
  assert.equal(password.value, "secret");
  assert.equal(button.textContent, "Show password");
  assert.equal(button.attributes["aria-pressed"], "false");
});
