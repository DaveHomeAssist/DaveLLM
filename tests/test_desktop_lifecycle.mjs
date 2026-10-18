import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { EventEmitter } from "node:events";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../desktop/main.js", import.meta.url), "utf8");

function desktop(hasLock, env = {}) {
  const calls = { ready: 0, quit: 0, spawned: 0, restored: 0, focused: 0 };
  const app = new EventEmitter();
  app.requestSingleInstanceLock = () => hasLock;
  app.quit = () => { calls.quit += 1; };
  // Keep startup pending: the loser must not even schedule backend startup.
  app.whenReady = () => { calls.ready += 1; return new Promise(() => {}); };
  let windows = [];
  const BrowserWindow = { getAllWindows: () => windows };
  vm.runInNewContext(source, {
    require(name) {
      if (name === "electron") return { app, BrowserWindow };
      if (name === "child_process") return { spawn() { calls.spawned += 1; } };
      if (name === "path") return path;
      if (name === "http") return {};
      throw new Error(`Unexpected dependency: ${name}`);
    },
    process: { env, platform: "darwin", cwd: () => "/fixture" },
    __dirname: "/fixture/desktop",
  });
  return { app, calls, setWindows(value) { windows = value; } };
}

test("second desktop exits before scheduling startup or spawning a backend", () => {
  const { app, calls } = desktop(false);
  assert.equal(calls.quit, 1);
  assert.equal(calls.ready, 0);
  assert.equal(calls.spawned, 0);
  app.emit("before-quit"); // No owned backend to kill.
});

test("primary desktop handles repeated launch without a second startup", () => {
  const { app, calls, setWindows } = desktop(true);
  assert.equal(calls.ready, 1);
  assert.equal(calls.quit, 0);
  app.emit("second-instance"); // Startup is still pending, no window yet.
  setWindows([{
    isMinimized: () => true,
    restore() { calls.restored += 1; },
    focus() { calls.focused += 1; },
  }]);
  app.emit("second-instance");
  assert.equal(calls.restored, 1);
  assert.equal(calls.focused, 1);
  assert.equal(calls.ready, 1);
  assert.equal(calls.spawned, 0);
});

test("managed desktop never owns or kills the backend", () => {
  const { app, calls } = desktop(true, { DAVE_BACKEND_MANAGED: "true" });
  assert.equal(calls.ready, 1);
  assert.equal(calls.spawned, 0);
  app.emit("before-quit");
  assert.equal(calls.spawned, 0);
});
