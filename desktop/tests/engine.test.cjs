"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const {
  engineExecutableName,
  findAvailablePort,
  resolveEngineExecutable,
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
