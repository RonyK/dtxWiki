"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");

const ENGINE_START_TIMEOUT_MS = 60_000;
// The runtime allows up to 10 seconds each for HTTP drain and worker shutdown.
const ENGINE_STOP_TIMEOUT_MS = 25_000;

function engineExecutableName(platform = process.platform) {
  return platform === "win32" ? "dtxwiki-engine.exe" : "dtxwiki-engine";
}

function resolveEngineExecutable({ isPackaged, resourcesPath, appPath, platform = process.platform }) {
  if (process.env.DTXWIKI_ENGINE_PATH) {
    return path.resolve(process.env.DTXWIKI_ENGINE_PATH);
  }
  const base = isPackaged ? path.join(resourcesPath, "engine") : path.join(appPath, "build", "engine");
  return path.join(base, engineExecutableName(platform));
}

async function findAvailablePort(preferredPort = 0) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", (error) => {
      if (preferredPort && error?.code === "EADDRINUSE") {
        void findAvailablePort().then(resolve, reject);
        return;
      }
      reject(error);
    });
    server.listen({ host: "127.0.0.1", port: preferredPort, exclusive: true }, () => {
      const address = server.address();
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(address.port);
      });
    });
  });
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitUntilReady(origin, child, timeoutMs = ENGINE_START_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      const reason = child.signalCode ? `signal ${child.signalCode}` : `code ${child.exitCode}`;
      throw new Error(`로컬 엔진이 준비되기 전에 종료되었습니다 (${reason}).`);
    }
    try {
      const response = await fetch(`${origin}/health/ready`, { signal: AbortSignal.timeout(2_000) });
      if (response.ok) {
        return;
      }
      lastError = new Error(`readiness HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(`로컬 엔진 시작 시간이 초과되었습니다: ${lastError?.message ?? "unknown error"}`);
}

async function startLocalEngine({
  executable,
  logsDirectory,
  environment = process.env,
  preferredPort = 21080,
  onSpawn,
}) {
  if (!fs.existsSync(executable)) {
    throw new Error(`dtxWiki 로컬 엔진을 찾을 수 없습니다: ${executable}`);
  }
  fs.mkdirSync(logsDirectory, { recursive: true });
  const logPath = path.join(logsDirectory, "desktop-engine.log");
  const logStream = fs.createWriteStream(logPath, { flags: "a", mode: 0o600 });
  const logFailure = new Promise((_resolve, reject) => {
    logStream.once("error", reject);
  });
  const port = await findAvailablePort(preferredPort);
  const origin = `http://127.0.0.1:${port}`;
  const child = spawn(executable, ["--host", "127.0.0.1", "--port", String(port)], {
    env: { ...environment, PYTHONUNBUFFERED: "1" },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stdout.pipe(logStream, { end: false });
  child.stderr.pipe(logStream, { end: false });
  const engine = { child, logPath, logStream, origin, port };
  const spawnError = new Promise((_resolve, reject) => {
    child.once("error", reject);
  });

  try {
    onSpawn?.(engine);
    await Promise.race([waitUntilReady(origin, child), spawnError, logFailure]);
  } catch (error) {
    await stopLocalEngine(engine);
    throw error;
  }
  return engine;
}

async function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.removeListener("exit", onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      resolve(true);
    };
    child.once("exit", onExit);
  });
}

async function stopLocalEngine(engine) {
  if (!engine) {
    return;
  }
  const { child, logStream } = engine;
  if (child.exitCode === null && child.signalCode === null) {
    try {
      child.stdin?.end("shutdown\n");
    } catch {
      // The process may already have closed its stdin while shutting down.
    }
    if (!(await waitForExit(child, ENGINE_STOP_TIMEOUT_MS))) {
      child.kill();
      await waitForExit(child, 2_000);
    }
  }
  child.stdout?.unpipe(logStream);
  child.stderr?.unpipe(logStream);
  if (logStream && !logStream.closed && !logStream.destroyed) {
    await new Promise((resolve) => {
      logStream.once("close", resolve);
      logStream.end();
    });
  }
}

module.exports = {
  engineExecutableName,
  findAvailablePort,
  resolveEngineExecutable,
  startLocalEngine,
  stopLocalEngine,
  waitUntilReady,
};
