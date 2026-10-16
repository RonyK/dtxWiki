"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const test = require("node:test");

const {
  createStartupProgress,
  engineExecutableName,
  findAvailablePort,
  observeEngine,
  resolveEngineExecutable,
  stopLocalEngine,
  waitUntilReady,
} = require("../src/engine.cjs");

test("engine executable names follow the host platform", () => {
  assert.equal(engineExecutableName("win32"), "dtxwiki-engine.exe");
  assert.equal(engineExecutableName("darwin"), "dtxwiki-engine");
});

test("packaged engine resolves from Electron resources", () => {
  assert.equal(
    resolveEngineExecutable({
      isPackaged: true,
      resourcesPath: path.join("root", "resources"),
      appPath: path.join("root", "app"),
      platform: "win32",
    }),
    path.join("root", "resources", "engine", "dtxwiki-engine.exe"),
  );
});

test("available port finder returns an ephemeral TCP port", async () => {
  const port = await findAvailablePort();
  assert.ok(Number.isInteger(port));
  assert.ok(port > 0 && port <= 65535);
});

test("available port finder honors an available preferred port", async () => {
  const preferred = await findAvailablePort();
  assert.equal(await findAvailablePort(preferred), preferred);
});

test("readiness fails immediately when the child exits by signal", async () => {
  await assert.rejects(
    waitUntilReady("http://127.0.0.1:1", { exitCode: null, signalCode: "SIGTERM" }, 10),
    /signal SIGTERM/,
  );
});

test("startup gives bootstrap, worker startup and HTTP readiness separate bounded budgets", () => {
  let now = 0;
  const progress = createStartupProgress(() => now);
  assert.equal(progress.deadline, 60_000);
  progress.advance("worker-ready");
  assert.equal(progress.phase, "bootstrap");
  now = 59_000;
  progress.advance("worker-starting");
  assert.equal(progress.deadline, 144_000);
  now = 118_000;
  progress.advance("worker-ready");
  assert.equal(progress.deadline, 178_000);
  now = 177_000;
  progress.advance("worker-ready");
  progress.advance("worker-starting");
  assert.equal(progress.deadline, 178_000);
});

test("a late progress message cannot revive an expired startup phase", () => {
  let now = 0;
  const progress = createStartupProgress(() => now);
  now = 60_000;
  progress.advance("worker-starting");
  assert.equal(progress.phase, "bootstrap");
  assert.equal(progress.deadline, 60_000);
});

function observedEngine({ ready = true } = {}) {
  const failures = [];
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  const engine = { child, ready, startup: createStartupProgress() };
  observeEngine(engine, (failed, error) => failures.push({ engine: failed, error }));
  return { engine, child, failures };
}

test("a runtime worker failure reports its structured reason once after stderr drains", () => {
  const { engine, child, failures } = observedEngine();
  const event = JSON.stringify({
    event: "dtxwiki-runtime",
    error: "dtxWiki background worker stopped unexpectedly (exit code 7); dtxWiki has stopped",
  });
  child.stdout.write("ordinary engine output\n" + event.slice(0, 30));
  child.stdout.write(event.slice(30) + "\n");
  child.emit("exit", 1, null);
  assert.equal(failures.length, 0);
  child.emit("close", 1, null);
  child.emit("close", 1, null);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].engine, engine);
  assert.match(failures[0].error.message, /worker stopped unexpectedly \(exit code 7\)/);
});

test("an engine killed without a runtime event reports the exit signal", () => {
  const { child, failures } = observedEngine();
  child.emit("close", null, "SIGKILL");
  assert.match(failures[0].error.message, /signal SIGKILL/);
});

test("readiness polling uses engine progress across slow bootstrap and worker startup", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  const { engine, child } = observedEngine();
  let healthCalls = 0;
  t.mock.method(global, "fetch", async () => ({ ok: ++healthCalls === 3, status: 503 }));
  const ready = waitUntilReady("http://127.0.0.1:21080", child, 60_000, engine);
  await Promise.resolve();
  t.mock.timers.tick(59_000);
  child.stdout.write(JSON.stringify({ event: "dtxwiki-runtime", phase: "worker-starting" }) + "\n");
  await Promise.resolve();
  await Promise.resolve();
  t.mock.timers.tick(59_000);
  child.stdout.write(JSON.stringify({ event: "dtxwiki-runtime", phase: "worker-ready" }) + "\n");
  await ready;
  assert.equal(healthCalls, 3);
  assert.equal(engine.startup.phase, "worker-ready");
});

test("startup failures remain in the startup error path", async () => {
  const { engine, child, failures } = observedEngine({ ready: false });
  child.stdout.write(JSON.stringify({ event: "dtxwiki-runtime", error: "worker failed to start" }) + "\n");
  child.exitCode = 1;
  child.emit("close", 1, null);
  assert.equal(failures.length, 0);
  await assert.rejects(waitUntilReady("http://127.0.0.1:1", child, 10, engine), /worker failed to start/);
});

test("intentional shutdown is marked before sending the shutdown message", async () => {
  const { engine, child, failures } = observedEngine();
  child.stdin = {
    end(message) {
      assert.equal(message, "shutdown\n");
      child.exitCode = 0;
      child.emit("exit", 0, null);
      child.emit("close", 0, null);
    },
  };
  await stopLocalEngine(engine);
  assert.equal(failures.length, 0);
  assert.equal(engine.stopping, true);
});
