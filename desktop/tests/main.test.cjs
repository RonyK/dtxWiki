"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function desktopFixture({ failDuringLoad = false } = {}) {
  const windows = [];
  const dialogs = [];
  const engines = [];
  const stopped = [];
  const workerError = new Error("dtxWiki background worker stopped unexpectedly (exit code 7)");
  class BrowserWindow extends EventEmitter {
    constructor() {
      super();
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      windows.push(this);
    }
    show() {}
    isDestroyed() { return Boolean(this.destroyed); }
    destroy() { this.destroyed = true; this.emit("closed"); }
    async loadFile(file) { this.file = file; this.emit("ready-to-show"); }
    async loadURL(url) {
      this.url = url;
      if (url === "https://unavailable.example") throw new Error("remote unavailable");
      if (failDuringLoad) {
        const engine = engines.at(-1);
        engine.onUnexpectedExit(engine, workerError);
        throw new Error("ERR_CONNECTION_REFUSED");
      }
    }
  }
  const mocks = {
    electron: {
      app: {
        requestSingleInstanceLock: () => false,
        quit: () => {},
        getPath: () => "/logs",
        getAppPath: () => "/app",
      },
      BrowserWindow,
      dialog: { showErrorBox: (...args) => dialogs.push(args) },
      Menu: { buildFromTemplate: (items) => items, setApplicationMenu: () => {} },
    },
    "electron-updater": {},
    "./config.cjs": {
      defaultConfig: () => ({
        localPort: 21080, lastConnection: null,
        remoteServers: [{ id: "remote", name: "Remote", url: "https://unavailable.example" }],
      }),
      writeConfig: (_file, config) => config,
    },
    "./engine.cjs": {
      resolveEngineExecutable: () => "/engine",
      async startLocalEngine(options) {
        const engine = { origin: "http://127.0.0.1:21080", port: 21080, logPath: "/logs/desktop-engine.log" };
        engine.onUnexpectedExit = options.onUnexpectedExit;
        engines.push(engine);
        options.onSpawn(engine);
        return engine;
      },
      async stopLocalEngine(engine) { stopped.push(engine); },
    },
    "./mcp.cjs": {},
    "./security.cjs": {},
    "./updater.cjs": {},
  };
  const filename = path.join(__dirname, "..", "src", "main.cjs");
  const context = vm.createContext({
    require: (name) => Object.hasOwn(mocks, name) ? mocks[name] : require(name),
    __dirname: path.dirname(filename),
    process,
    URL,
    module: { exports: {} },
  });
  vm.runInContext(
    fs.readFileSync(filename, "utf8") + "\nmodule.exports = { connectLocal, connectRemote, showWelcome };",
    context,
    { filename },
  );
  return { ...context.module.exports, dialogs, engines, stopped, windows, workerError };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a running local engine failure shows its reason once and returns to connection screen", async () => {
  const desktop = desktopFixture();
  await desktop.connectLocal();
  const engine = desktop.engines[0];
  engine.onUnexpectedExit(engine, desktop.workerError);
  engine.onUnexpectedExit(engine, desktop.workerError);
  await settle();
  assert.equal(desktop.dialogs.length, 1);
  assert.match(desktop.dialogs[0][1], /worker stopped unexpectedly \(exit code 7\)/);
  assert.match(desktop.dialogs[0][1], /desktop-engine\.log/);
  assert.equal(desktop.stopped.filter((item) => item === engine).length, 1);
  assert.match(desktop.windows.at(-1).file, /welcome[/\\]index\.html$/);
});

test("an engine failure during page loading does not also show a startup dialog", async () => {
  const desktop = desktopFixture({ failDuringLoad: true });
  await desktop.connectLocal();
  await settle();
  assert.equal(desktop.dialogs.length, 1);
  assert.match(desktop.dialogs[0][1], /worker stopped unexpectedly/);
  assert.match(desktop.windows.at(-1).file, /welcome[/\\]index\.html$/);
});

test("an old engine cannot report a failure after switching connections", async () => {
  const desktop = desktopFixture();
  await desktop.connectLocal();
  const oldEngine = desktop.engines[0];
  await desktop.connectLocal();
  oldEngine.onUnexpectedExit(oldEngine, desktop.workerError);
  await settle();
  assert.equal(desktop.dialogs.length, 0);
  assert.equal(desktop.windows.at(-1).url, desktop.engines[1].origin);
});

test("returning to the connection screen suppresses intentional shutdown failures", async () => {
  const desktop = desktopFixture();
  await desktop.connectLocal();
  const engine = desktop.engines[0];
  await desktop.showWelcome();
  engine.onUnexpectedExit(engine, desktop.workerError);
  await settle();
  assert.equal(desktop.dialogs.length, 0);
});

test("the active local engine still reports failure after a remote connection attempt fails", async () => {
  const desktop = desktopFixture();
  await desktop.connectLocal();
  await assert.rejects(desktop.connectRemote("remote"), /remote unavailable/);
  const engine = desktop.engines[0];
  engine.onUnexpectedExit(engine, desktop.workerError);
  await settle();
  assert.equal(desktop.dialogs.length, 1);
  assert.match(desktop.dialogs[0][1], /worker stopped unexpectedly/);
});
