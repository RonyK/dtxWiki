"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { canWriteClipboard, isAllowedNavigation } = require("../src/security.cjs");

test("trusted file pages are pinned to their exact URL", () => {
  const welcome = "file:///C:/app/src/welcome/index.html";
  assert.equal(isAllowedNavigation(welcome, welcome), true);
  assert.equal(isAllowedNavigation("file:///C:/Windows/win.ini", welcome), false);
});

test("network pages may navigate only within their server origin", () => {
  assert.equal(
    isAllowedNavigation("https://wiki.example.com/context", "https://wiki.example.com"),
    true,
  );
  assert.equal(
    isAllowedNavigation("https://attacker.example/context", "https://wiki.example.com"),
    false,
  );
});

test("clipboard writes are limited to the visible configured wiki", () => {
  const webContents = { getURL: () => "https://wiki.example.com/context" };
  const mainWindow = { isDestroyed: () => false, webContents };
  const base = {
    webContents,
    mainWindow,
    localOrigin: null,
    remoteOrigins: new Set(["https://wiki.example.com"]),
  };
  assert.equal(
    canWriteClipboard({
      ...base,
      permission: "clipboard-sanitized-write",
      requestingUrl: "https://wiki.example.com/context",
    }),
    true,
  );
  assert.equal(
    canWriteClipboard({
      ...base,
      permission: "clipboard-read",
      requestingUrl: "https://wiki.example.com/context",
    }),
    false,
  );
  assert.equal(
    canWriteClipboard({
      ...base,
      permission: "clipboard-sanitized-write",
      requestingUrl: "https://attacker.example/",
    }),
    false,
  );
});
