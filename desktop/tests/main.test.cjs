"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function desktopFixture({ failDuringLoad = false, singleInstance = false, stopEngine, afterSpawn } = {}) {
  const windows = [];
  const dialogs = [];
  const engines = [];
  const menus = [];
  const stopped = [];
  const workerError = new Error("dtxWiki background worker stopped unexpectedly (exit code 7)");
  const app = new EventEmitter();
  let quits = 0;
  Object.assign(app, {
    requestSingleInstanceLock: () => singleInstance,
    quit: () => { quits += 1; },
    getPath: () => "/logs",
    getAppPath: () => "/app",
    whenReady: () => new Promise(() => {}),
  });
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
      app,
      BrowserWindow,
      dialog: { showErrorBox: (...args) => dialogs.push(args) },
      Menu: { buildFromTemplate: (items) => items, setApplicationMenu: (menu) => menus.push(menu) },
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
        await afterSpawn?.(engine, engines.length);
        return engine;
      },
      async stopLocalEngine(engine) { stopped.push(engine); await stopEngine?.(engine); },
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
    fs.readFileSync(filename, "utf8") + "\nmodule.exports = { connectLocal, connectRemote, showWelcome, stopAllEngines, state: () => ({ localEngine, pendingEngine, engines: [...engineRegistry], quitting, quitAuthorized }) };",
    context,
    { filename },
  );
  return { ...context.module.exports, app, dialogs, engines, stopped, windows, menus, workerError, quits: () => quits };
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

test("engine references survive a stop failure and prevent a new local start", async () => {
  let fail = true;
  const desktop = desktopFixture({ stopEngine: async () => { if (fail) throw new Error("unconfirmed PID 1234"); } });
  await desktop.connectLocal();
  const engine = desktop.engines[0];
  await assert.rejects(desktop.connectLocal(), /unconfirmed PID/);
  assert.equal(desktop.engines.length, 1);
  assert.equal(desktop.state().localEngine, engine);
  assert.ok(desktop.state().engines.includes(engine));
  fail = false;
  await desktop.stopAllEngines();
  assert.equal(desktop.state().localEngine, null);
  assert.equal(desktop.state().engines.length, 0);
});

test("switching connections keeps the engine reference until stop completes", async () => {
  let confirmExit;
  const exited = new Promise((resolve) => { confirmExit = resolve; });
  const desktop = desktopFixture({ stopEngine: () => exited });
  await desktop.connectLocal();
  const engine = desktop.engines[0];
  const switching = desktop.showWelcome();
  await settle();
  assert.equal(desktop.state().localEngine, engine);
  assert.ok(desktop.state().engines.includes(engine));
  assert.ok(!desktop.windows.at(-1).file.endsWith("index.html"));
  confirmExit();
  await switching;
  assert.equal(desktop.state().localEngine, null);
  assert.equal(desktop.state().engines.length, 0);
  assert.match(desktop.windows.at(-1).file, /welcome[/\\]index\.html$/);
});

test("repeated quit after failed stop cannot bypass engine shutdown", async () => {
  let fail = true;
  const desktop = desktopFixture({ singleInstance: true, stopEngine: async () => { if (fail) throw new Error("unconfirmed PID 1234"); } });
  await desktop.connectLocal();
  let prevented = 0;
  const quit = () => desktop.app.emit("before-quit", { preventDefault: () => { prevented += 1; } });
  quit();
  quit();
  await settle();
  assert.equal(prevented, 2);
  assert.equal(desktop.quits(), 0);
  assert.equal(desktop.state().quitAuthorized, false);
  assert.equal(desktop.state().engines.length, 1);
  quit();
  await settle();
  assert.equal(prevented, 3);
  assert.equal(desktop.quits(), 0);
  assert.equal(desktop.dialogs.length, 2);
  fail = false;
  quit();
  await settle();
  assert.equal(desktop.quits(), 1);
  assert.equal(desktop.state().quitAuthorized, true);
  quit();
  assert.equal(prevented, 4);
});

test("a stale start resolving later cannot replace or stop the current engine", async () => {
  let releaseFirst;
  const first = new Promise((resolve) => { releaseFirst = resolve; });
  const desktop = desktopFixture({ afterSpawn: (_engine, number) => number === 1 ? first : undefined });
  const firstConnection = desktop.connectLocal();
  await settle();
  await desktop.connectLocal();
  const current = desktop.engines[1];
  assert.equal(desktop.state().localEngine, current);
  releaseFirst();
  await firstConnection;
  assert.equal(desktop.state().localEngine, current);
  assert.ok(!desktop.stopped.includes(current));
});

test("menu connection changes report stop rejection and retain the live engine", async () => {
  const desktop = desktopFixture({ stopEngine: async () => { throw new Error("unconfirmed PID 1234"); } });
  await desktop.connectLocal();
  const engine = desktop.engines[0];
  const connection = desktop.menus.at(-1).find((item) => item.label === "연결");
  connection.submenu.find((item) => item.label === "연결 관리…").click();
  await settle();
  assert.equal(desktop.dialogs.length, 1);
  assert.match(desktop.dialogs[0][1], /unconfirmed PID 1234/);
  assert.equal(desktop.state().localEngine, engine);
});
