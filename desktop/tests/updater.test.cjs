"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { createUpdateManager } = require("../src/updater.cjs");

function fixture({ packaged = true, response = 1, beforeInstall = async () => {} } = {}) {
  const updater = new EventEmitter();
  updater.checkForUpdates = async () => {};
  updater.downloadUpdate = async () => {
    updater.downloaded = true;
  };
  updater.quitAndInstall = () => {
    updater.installed = true;
  };
  const messages = [];
  const manager = createUpdateManager({
    app: { isPackaged: packaged },
    autoUpdater: updater,
    dialog: {
      showMessageBox: async (...args) => {
        messages.push(args.at(-1));
        return { response };
      },
    },
    getWindow: () => null,
    beforeInstall,
  });
  return { manager, messages, updater };
}

test("development builds do not contact an update feed", async () => {
  const { manager, messages, updater } = fixture({ packaged: false });
  let checked = false;
  updater.checkForUpdates = async () => {
    checked = true;
  };
  await manager.check({ manual: true });
  assert.equal(checked, false);
  assert.match(messages[0].message, /개발 빌드/);
});

test("an accepted update is downloaded", async () => {
  const { updater } = fixture({ response: 0 });
  updater.emit("update-available", { version: "1.2.3" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(updater.downloaded, true);
});

test("installation is held until engine exit and is cancelled when stop fails", async () => {
  const { updater, messages } = fixture({
    response: 0,
    beforeInstall: async () => { throw new Error("unconfirmed PID 1234"); },
  });
  updater.emit("update-downloaded", { version: "1.2.3" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(updater.installed, undefined);
  assert.equal(messages.at(-1).type, "error");
  assert.match(messages.at(-1).detail, /unconfirmed PID 1234/);
});

test("installation awaits the shared engine stop before quitting", async () => {
  let confirmExit;
  const exited = new Promise((resolve) => { confirmExit = resolve; });
  const { updater } = fixture({ response: 0, beforeInstall: () => exited });
  updater.emit("update-downloaded", { version: "1.2.3" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(updater.installed, undefined);
  confirmExit();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(updater.installed, true);
});
