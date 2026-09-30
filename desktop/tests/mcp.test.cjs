"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {
  authenticationPolicy,
  registrationSummary,
} = require("../src/mcp/policy.js");

function source(relativePath) {
  return fs.readFileSync(path.join(__dirname, "..", relativePath), "utf8");
}

test("MCP wizard only submits saved HTTP server registrations", () => {
  const html = source("src/mcp/index.html");
  const script = source("src/mcp/wizard.js");
  const main = source("src/main.cjs");
  const engine = source("engine_entry.py");

  assert.doesNotMatch(html, /include-local|stdio/i);
  assert.doesNotMatch(script, /includeLocal|claude-desktop|cursor/i);
  assert.doesNotMatch(main, /includeLocal/);
  assert.doesNotMatch(engine, /mcp-stdio|DTXWIKI_DESKTOP_MCP_COMMAND/);
  assert.match(html, /Streamable HTTP MCP/);
  assert.match(html, /policy\.js/);
  assert.match(script, /registrationSummary\(result\)/);
});

test("MCP wizard requires PAT for one plaintext loopback server", () => {
  assert.deepEqual(authenticationPolicy([{ url: "http://127.0.0.1:21080" }]), {
    requiresPat: true,
    oauthAllowed: false,
    patAllowed: true,
    preferredAuth: "pat",
    blockedReason: null,
  });
});

test("MCP wizard blocks a mixed batch that cannot share an auth mode", () => {
  const policy = authenticationPolicy([
    { url: "http://localhost:21080" },
    { url: "https://wiki.example.com" },
  ]);

  assert.equal(policy.oauthAllowed, false);
  assert.equal(policy.patAllowed, false);
  assert.match(policy.blockedReason, /PAT.*하나씩/);
});

test("MCP wizard keeps OAuth available for HTTPS servers", () => {
  const policy = authenticationPolicy([
    { url: "https://one.example.com" },
    { url: "https://two.example.com" },
  ]);

  assert.equal(policy.requiresPat, false);
  assert.equal(policy.oauthAllowed, true);
  assert.equal(policy.preferredAuth, "oauth");
});

test("MCP wizard reports exact deduplicated secret environment setup", () => {
  const summary = registrationSummary({
    config_writes_complete: true,
    configuration_failures: [],
    setup_required: true,
    secret_env_names: ["SECOND_PAT", "DTXWIKI_MCP_PAT"],
    registrations: [
      { wrote_config: true, secret_env_names: ["DTXWIKI_MCP_PAT"] },
      { wrote_config: true, secret_env_names: ["SECOND_PAT"] },
    ],
  });

  assert.deepEqual(summary, {
    registrationCount: 2,
    wroteConfigCount: 2,
    setupRequired: true,
    secretEnvNames: ["DTXWIKI_MCP_PAT", "SECOND_PAT"],
    configurationFailures: [],
    status: "setup-required",
  });
  const script = source("src/mcp/wizard.js");
  assert.match(script, /추가 인증 설정 필요/);
  assert.match(script, /시스템 또는 전역 환경 변수에 저장하지 않습니다/);
});

test("MCP wizard fails closed on inconsistent helper status", () => {
  assert.throws(
    () =>
      registrationSummary({
        config_writes_complete: true,
        configuration_failures: [],
        setup_required: true,
        registrations: [],
      }),
    /inconsistent secret setup status/,
  );
});

test("MCP wizard reports partial config failures without claiming completion", () => {
  const summary = registrationSummary({
    config_writes_complete: false,
    configuration_failures: [
      {
        client: "gemini-cli",
        config_path: "C:/Users/test/.gemini/settings.json",
        reason: "config patch failed: permission denied",
      },
    ],
    setup_required: true,
    secret_env_names: ["DTXWIKI_MCP_PAT"],
    registrations: [
      { client: "codex", wrote_config: true, secret_env_names: ["DTXWIKI_MCP_PAT"] },
      { client: "gemini-cli", wrote_config: false, secret_env_names: ["DTXWIKI_MCP_PAT"] },
    ],
  });

  assert.equal(summary.status, "partial-failure");
  assert.equal(summary.wroteConfigCount, 1);
  assert.equal(summary.configurationFailures.length, 1);
  assert.match(summary.configurationFailures[0].reason, /permission denied/);
  const script = source("src/mcp/wizard.js");
  assert.match(script, /일부 MCP 설정 실패/);
  assert.match(script, /이미 반영된 다른 클라이언트 설정은 유지되었습니다/);
});

test("failed config writes do not create a false environment setup requirement", () => {
  const summary = registrationSummary({
    config_writes_complete: false,
    configuration_failures: [
      { client: "codex", config_path: "config.toml", reason: "permission denied" },
    ],
    setup_required: false,
    secret_env_names: [],
    registrations: [
      { client: "codex", wrote_config: false, secret_env_names: ["DTXWIKI_MCP_PAT"] },
    ],
  });

  assert.equal(summary.status, "failed");
  assert.equal(summary.setupRequired, false);
  assert.deepEqual(summary.secretEnvNames, []);
});
