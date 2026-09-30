"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const CONFIG_VERSION = 1;
const DEFAULT_LOCAL_PORT = 21080;

function defaultConfig() {
  return {
    version: CONFIG_VERSION,
    localPort: DEFAULT_LOCAL_PORT,
    lastConnection: null,
    remoteServers: [],
  };
}

function isLoopback(hostname) {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

function normalizeRemoteUrl(rawValue) {
  const value = String(rawValue ?? "").trim();
  if (!value) {
    throw new Error("서버 URL을 입력하세요.");
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("올바른 서버 URL을 입력하세요. 예: https://wiki.example.com");
  }

  if (parsed.username || parsed.password) {
    throw new Error("URL에 사용자 이름이나 비밀번호를 포함할 수 없습니다.");
  }
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopback(parsed.hostname))) {
    throw new Error("원격 서버는 HTTPS를 사용해야 합니다.");
  }
  if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error("dtxWiki 서버의 origin만 입력하세요. 경로, 쿼리, 조각은 사용할 수 없습니다.");
  }

  return parsed.origin;
}

function normalizeServerName(rawName, url) {
  const requested = String(rawName ?? "").trim();
  if (requested) {
    return requested.slice(0, 80);
  }
  return new URL(url).host;
}

function mcpNameForServer(name, url, existingNames = []) {
  const base = String(name || new URL(url).host)
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase()
    .slice(0, 48) || "remote";
  return uniqueMcpName(`dtxwiki-${base}`, existingNames);
}

function uniqueMcpName(base, existingNames = []) {
  const reserved = new Set(["dtxwiki", ...existingNames]);
  let candidate = base.slice(0, 80);
  let suffix = 2;
  while (reserved.has(candidate)) {
    const suffixText = `-${suffix}`;
    candidate = `${base.slice(0, 80 - suffixText.length)}${suffixText}`;
    suffix += 1;
  }
  return candidate;
}

function canonicalMcpName(rawName, name, url, existingNames = []) {
  const requested = String(rawName ?? "")
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!requested || requested === "dtxwiki") {
    return mcpNameForServer(name, url, existingNames);
  }
  return uniqueMcpName(requested, existingNames);
}

function sanitizeConfig(value) {
  const result = defaultConfig();
  if (!value || typeof value !== "object" || value.version !== CONFIG_VERSION) {
    return result;
  }

  if (Number.isInteger(value.localPort) && value.localPort >= 1024 && value.localPort <= 65535) {
    result.localPort = value.localPort;
  }

  const seen = new Set();
  const seenMcpNames = new Set();
  for (const candidate of Array.isArray(value.remoteServers) ? value.remoteServers : []) {
    try {
      const url = normalizeRemoteUrl(candidate.url);
      if (seen.has(url)) {
        continue;
      }
      seen.add(url);
      const mcpName = canonicalMcpName(
        candidate.mcpName,
        candidate.name,
        url,
        seenMcpNames,
      );
      seenMcpNames.add(mcpName);
      result.remoteServers.push({
        id: typeof candidate.id === "string" && candidate.id ? candidate.id : crypto.randomUUID(),
        name: normalizeServerName(candidate.name, url),
        url,
        mcpName,
      });
    } catch {
      // Ignore corrupt or obsolete entries without making the app unstartable.
    }
  }

  if (value.lastConnection?.type === "local") {
    result.lastConnection = { type: "local" };
  } else if (value.lastConnection?.type === "remote") {
    const server = result.remoteServers.find((item) => item.id === value.lastConnection.serverId);
    if (server) {
      result.lastConnection = { type: "remote", serverId: server.id };
    }
  }
  return result;
}

function readConfig(configPath) {
  try {
    return sanitizeConfig(JSON.parse(fs.readFileSync(configPath, "utf8")));
  } catch (error) {
    if (error?.code === "ENOENT") {
      return defaultConfig();
    }
    return defaultConfig();
  }
}

function writeConfig(configPath, config) {
  const normalized = sanitizeConfig(config);
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const temporary = `${configPath}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(normalized, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    fs.renameSync(temporary, configPath);
  } finally {
    try {
      fs.unlinkSync(temporary);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
  }
  return normalized;
}

function upsertRemoteServer(config, { name, url }) {
  const normalizedUrl = normalizeRemoteUrl(url);
  const existing = config.remoteServers.find((item) => item.url === normalizedUrl);
  if (existing) {
    existing.name = normalizeServerName(name, normalizedUrl);
    existing.mcpName = canonicalMcpName(
      existing.mcpName,
      existing.name,
      normalizedUrl,
      config.remoteServers.filter((item) => item !== existing).map((item) => item.mcpName),
    );
    return existing;
  }
  const server = {
    id: crypto.randomUUID(),
    name: normalizeServerName(name, normalizedUrl),
    url: normalizedUrl,
    mcpName: mcpNameForServer(
      name,
      normalizedUrl,
      config.remoteServers.map((item) => item.mcpName),
    ),
  };
  config.remoteServers.push(server);
  return server;
}

module.exports = {
  CONFIG_VERSION,
  DEFAULT_LOCAL_PORT,
  canonicalMcpName,
  defaultConfig,
  normalizeRemoteUrl,
  mcpNameForServer,
  readConfig,
  sanitizeConfig,
  upsertRemoteServer,
  writeConfig,
};
