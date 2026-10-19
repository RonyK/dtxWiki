"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");

const ENGINE_START_TIMEOUT_MS = 60_000;
// The runtime allows up to 10 seconds each for HTTP drain and worker shutdown.
const ENGINE_STOP_TIMEOUT_MS = 25_000;
const ENGINE_TERM_TIMEOUT_MS = 2_000;
const ENGINE_KILL_TIMEOUT_MS = 2_000;
// Allow the runtime's bounded worker startup and its cleanup to finish before
// enforcing the desktop deadline, so its failure reason can reach the shell.
const ENGINE_WORKER_START_TIMEOUT_MS = 60_000 + ENGINE_STOP_TIMEOUT_MS;

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

function createStartupProgress(now = Date.now) {
  const progress = { phase: "bootstrap", deadline: now() + ENGINE_START_TIMEOUT_MS };
  progress.advance = (phase) => {
    if (now() >= progress.deadline) return;
    if (progress.phase === "bootstrap" && phase === "worker-starting") {
      progress.phase = phase;
      progress.deadline = now() + ENGINE_WORKER_START_TIMEOUT_MS;
    } else if (progress.phase === "worker-starting" && phase === "worker-ready") {
      progress.phase = phase;
      progress.deadline = now() + ENGINE_START_TIMEOUT_MS;
    }
  };
  return progress;
}

function observeEngine(engine, onUnexpectedExit) {
  let pending = "";
  engine.child.stdout.on("data", (chunk) => {
    pending += chunk.toString();
    let newline;
    while ((newline = pending.indexOf("\n")) !== -1) {
      const line = pending.slice(0, newline);
      pending = pending.slice(newline + 1);
      try {
        const event = JSON.parse(line);
        if (event.event !== "dtxwiki-runtime") continue;
        if (typeof event.error === "string") engine.runtimeError = event.error.slice(0, 4096);
        engine.startup.advance(event.phase);
      } catch {
        // Ordinary engine output remains in the log, outside this protocol.
      }
    }
    // A malformed or unterminated log line must not accumulate indefinitely.
    if (pending.length > 16_384) pending = "";
  });
  engine.child.once("close", (code, signal) => {
    const reason = signal ? `signal ${signal}` : `code ${code}`;
    engine.exitError = new Error(
      engine.runtimeError || `로컬 엔진이 예기치 않게 종료되었습니다 (${reason}).`,
    );
    if (engine.ready && !engine.stopping) onUnexpectedExit?.(engine, engine.exitError);
  });
}

async function waitUntilReady(
  origin,
  child,
  timeoutMs = ENGINE_START_TIMEOUT_MS,
  engine = null,
) {
  const startedAt = Date.now();
  const deadline = () => engine?.startup.deadline ?? startedAt + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline()) {
    if (child.exitCode !== null || child.signalCode !== null) {
      const reason = child.signalCode ? `signal ${child.signalCode}` : `code ${child.exitCode}`;
      throw new Error(engine?.runtimeError || `로컬 엔진이 준비되기 전에 종료되었습니다 (${reason}).`);
    }
    try {
      const response = await fetch(`${origin}/health/ready`, { signal: AbortSignal.timeout(2_000) });
      if (response.ok) {
        if (child.exitCode !== null || child.signalCode !== null) continue;
        return;
      }
      lastError = new Error(`readiness HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(
    engine?.runtimeError ||
      `로컬 엔진 시작 시간이 초과되었습니다 (${engine?.startup.phase ?? "startup"}): ${lastError?.message ?? "unknown error"}`,
  );
}

async function startLocalEngine({
  executable,
  logsDirectory,
  environment = process.env,
  preferredPort = 21080,
  onSpawn,
  onUnexpectedExit,
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
  child.stdout.setEncoding("utf8");
  child.stdout.pipe(logStream, { end: false });
  child.stderr.pipe(logStream, { end: false });
  const engine = {
    child, logPath, logStream, origin, port,
    startup: createStartupProgress(), ready: false, stopping: false,
  };
  observeEngine(engine, onUnexpectedExit);
  const spawnError = new Promise((_resolve, reject) => {
    child.once("error", (error) => {
      engine.spawnError = error;
      reject(error);
    });
  });

  try {
    onSpawn?.(engine);
    await Promise.race([waitUntilReady(origin, child, ENGINE_START_TIMEOUT_MS, engine), spawnError, logFailure]);
    engine.ready = true;
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

async function stopEngine(engine) {
  engine.stopping = true;
  const { child, logStream } = engine;
  const neverSpawned = engine.spawnError && child.pid === undefined;
  if (!neverSpawned && child.exitCode === null && child.signalCode === null) {
    try {
      child.stdin?.end("shutdown\n");
    } catch {
      // The process may already have closed its stdin while shutting down.
    }
    if (!(await waitForExit(child, ENGINE_STOP_TIMEOUT_MS))) {
      try { child.kill("SIGTERM"); } catch { /* Exit confirmation decides success. */ }
      if (!(await waitForExit(child, ENGINE_TERM_TIMEOUT_MS))) {
        try { child.kill("SIGKILL"); } catch { /* Keep the engine reference on failure. */ }
        if (!(await waitForExit(child, ENGINE_KILL_TIMEOUT_MS))) {
          throw new Error(
            `로컬 엔진 종료를 확인하지 못했습니다 (PID ${child.pid ?? "unknown"}). 로그: ${engine.logPath}`,
          );
        }
      }
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

function stopLocalEngine(engine) {
  if (!engine) return Promise.resolve();
  if (engine.stopPromise) return engine.stopPromise;
  const stopping = stopEngine(engine);
  engine.stopPromise = stopping;
  // Concurrent callers share one attempt. A failed attempt may be retried,
  // while its process and log stream remain owned by the caller.
  void stopping.catch(() => {
    if (engine.stopPromise === stopping) engine.stopPromise = null;
  });
  return stopping;
}

module.exports = {
  createStartupProgress,
  engineExecutableName,
  findAvailablePort,
  observeEngine,
  resolveEngineExecutable,
  startLocalEngine,
  stopLocalEngine,
  waitUntilReady,
};
