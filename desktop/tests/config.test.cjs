"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  canonicalMcpName,
  defaultConfig,
  mcpNameForServer,
  normalizeRemoteUrl,
  readConfig,
  sanitizeConfig,
  upsertRemoteServer,
  writeConfig,
} = require("../src/config.cjs");

test("remote servers require HTTPS except for loopback development", () => {
  assert.equal(normalizeRemoteUrl("https://wiki.example.com/"), "https://wiki.example.com");
  assert.equal(normalizeRemoteUrl("http://127.0.0.1:21080"), "http://127.0.0.1:21080");
  assert.throws(() => normalizeRemoteUrl("http://wiki.example.com"), /HTTPS/);
  assert.throws(() => normalizeRemoteUrl("https://user:secret@wiki.example.com"), /비밀번호/);
  assert.throws(() => normalizeRemoteUrl("https://wiki.example.com/context"), /origin/);
});

test("local engine port is stable and invalid persisted values are repaired", () => {
  assert.equal(defaultConfig().localPort, 21080);
  assert.equal(sanitizeConfig({ version: 1, localPort: 23111 }).localPort, 23111);
  assert.equal(sanitizeConfig({ version: 1, localPort: 80 }).localPort, 21080);
});

test("config sanitization drops invalid and duplicate servers", () => {
  const config = sanitizeConfig({
    version: 1,
    lastConnection: { type: "remote", serverId: "kept" },
    remoteServers: [
      { id: "kept", name: "Team", url: "https://wiki.example.com" },
      { id: "duplicate", name: "Duplicate", url: "https://wiki.example.com/" },
      { id: "unsafe", name: "Unsafe", url: "http://wiki.example.com" },
    ],
  });
  assert.deepEqual(config.remoteServers, [
    {
      id: "kept",
      name: "Team",
      url: "https://wiki.example.com",
      mcpName: "dtxwiki-team",
    },
  ]);
  assert.deepEqual(config.lastConnection, { type: "remote", serverId: "kept" });
});

test("remote profiles receive stable unique MCP names", () => {
  assert.equal(mcpNameForServer("회사 위키", "https://wiki.example.com"), "dtxwiki-remote");
  assert.equal(
    mcpNameForServer("Team", "https://wiki.example.com", ["dtxwiki-team"]),
    "dtxwiki-team-2",
  );
});

test("MCP names use the common client-safe alphabet", () => {
  assert.equal(
    mcpNameForServer("Team_alpha", "https://wiki.example.com"),
    "dtxwiki-team-alpha",
  );
  assert.match(
    canonicalMcpName("TEAM_alpha", "Team", "https://wiki.example.com"),
    /^[A-Za-z0-9-]+$/,
  );
});

test("config sanitization migrates underscore names with deterministic collisions", () => {
  const config = sanitizeConfig({
    version: 1,
    remoteServers: [
      {
        id: "legacy",
        name: "Legacy",
        url: "https://legacy.example.com",
        mcpName: "dtxwiki_team",
      },
      {
        id: "current",
        name: "Current",
        url: "https://current.example.com",
        mcpName: "dtxwiki-team",
      },
      {
        id: "third",
        name: "Third",
        url: "https://third.example.com",
        mcpName: "dtxwiki_team",
      },
    ],
  });

  assert.deepEqual(
    config.remoteServers.map((server) => server.mcpName),
    ["dtxwiki-team", "dtxwiki-team-2", "dtxwiki-team-3"],
  );
  assert.ok(config.remoteServers.every((server) => /^[A-Za-z0-9-]+$/.test(server.mcpName)));
});

test("remote upsert reuses a normalized origin", () => {
  const config = defaultConfig();
  const first = upsertRemoteServer(config, { name: "Team", url: "https://wiki.example.com" });
  const second = upsertRemoteServer(config, { name: "Renamed", url: "https://wiki.example.com/" });
  assert.equal(first.id, second.id);
  assert.equal(config.remoteServers.length, 1);
  assert.equal(config.remoteServers[0].name, "Renamed");
});

test("config round-trips through a private JSON file", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "dtxwiki-desktop-"));
  try {
    const configPath = path.join(directory, "config.json");
    const config = defaultConfig();
    const server = upsertRemoteServer(config, { name: "Team", url: "https://wiki.example.com" });
    config.lastConnection = { type: "remote", serverId: server.id };
    writeConfig(configPath, config);
    assert.deepEqual(readConfig(configPath), config);

    config.lastConnection = { type: "local" };
    writeConfig(configPath, config);
    assert.deepEqual(readConfig(configPath).lastConnection, { type: "local" });
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
