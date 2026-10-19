"use strict";

// Opt-in real-engine regression: npm --prefix desktop run test:engine-stop.
// Linux /proc confirms the actual init child opened the locked database;
// a delay or signal-handler observation alone does not establish that fact.
const assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { findAvailablePort, stopLocalEngine } = require("../src/engine.cjs");

const root = path.resolve(__dirname, "../..");
const python = path.join(root, ".venv", "bin", "python");
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function sourceIdentity() {
  const digest = createHash("sha256");
  for (const name of [
    "backend/app/runtime.py", "backend/app/runtime_init.py", "backend/app/runtime_protocol.py",
    "backend/app/runtime_resources.py", "backend/app/runtime_credentials.py", "backend/app/bootstrap.py",
    "backend/app/process_tree.py", "backend/services/ops/runtime_lock.py", "backend/worker/managed.py",
    "backend/worker/entry.py", "desktop/engine_entry.py", "desktop/src/engine.cjs",
    "desktop/tests/blocked-engine.integration.cjs",
  ]) {
    digest.update(name + "\0");
    digest.update(fs.readFileSync(path.join(root, name)));
  }
  return {
    commit: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
    fixture_and_runtime_sha256: digest.digest("hex"),
  };
}

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch (error) {
    if (error.code === "ESRCH") return false;
    throw error;
  }
}

function children(pid) {
  try {
    return fs.readFileSync(`/proc/${pid}/task/${pid}/children`, "utf8").trim().split(/\s+/).filter(Boolean).map(Number);
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return [];
    throw error;
  }
}

function holdsDatabase(pid, database) {
  try {
    return fs.readdirSync(`/proc/${pid}/fd`).some((descriptor) => {
      try { return fs.readlinkSync(`/proc/${pid}/fd/${descriptor}`) === database; } catch { return false; }
    });
  } catch { return false; }
}

async function until(check, description, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    // Poll explicit FD/process state; elapsed time never establishes success.
    await sleep(20);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function reaped(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve) => child.once("exit", resolve));
}

for (const autoBootstrap of [true, false]) {
  test(`Desktop stops actual DB-blocked init and restarts (auto_bootstrap=${autoBootstrap})`, {
    skip: process.platform !== "linux",
    timeout: 70_000,
  }, async (t) => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "dtxwiki-blocked-engine-"));
    const database = path.join(scratch, "wiki.sqlite");
    const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("DTXWIKI_")));
    Object.assign(environment, {
      DTXWIKI_DATA_DIR: path.join(scratch, "data"),
      DTXWIKI_DATABASE_URL: `sqlite:///${database}?timeout=600`,
      PYTHONPATH: root,
      PYTHONUNBUFFERED: "1",
    });
    const engines = [];
    const initPids = new Set();
    let holder;
    t.after(async () => {
      for (const child of engines) {
        if (alive(child.pid)) child.kill("SIGKILL");
        await reaped(child);
      }
      for (const pid of initPids) {
        if (alive(pid)) process.kill(pid, "SIGKILL");
      }
      if (holder) {
        if (alive(holder.pid)) holder.kill("SIGKILL");
        await reaped(holder);
      }
      fs.rmSync(scratch, { recursive: true, force: true });
    });
    execFileSync(python, ["-m", "backend.cli.main", "bootstrap", "--db", environment.DTXWIKI_DATABASE_URL], {
      env: environment, cwd: root, stdio: "pipe", timeout: 30_000,
    });
    holder = spawn(python, ["-c", [
      "import json, sqlite3, sys",
      "db=sqlite3.connect(sys.argv[1])",
      "revision=db.execute('SELECT version_num FROM alembic_version').fetchone()[0]",
      "version=db.execute(\"SELECT value FROM app_metadata WHERE key='dtxwiki_version'\").fetchone()[0]",
      "journal=db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]",
      "assert journal=='delete', journal",
      "db.execute('BEGIN EXCLUSIVE')",
      "print(json.dumps({'locked':True,'revision':revision,'version':version,'journal':journal}), flush=True)",
      "sys.stdin.read()",
      "db.rollback()",
    ].join(";"), database], { stdio: ["pipe", "pipe", "inherit"] });
    const fixture = await new Promise((resolve, reject) => {
      let output = "";
      holder.stdout.on("data", (data) => {
        output += data;
        if (output.includes("\n")) resolve(JSON.parse(output.split("\n")[0]));
      });
      holder.once("error", reject);
      holder.once("exit", (code) => reject(new Error(`SQLite holder exited ${code}`)));
    });
    assert.equal(fixture.locked, true);
    t.diagnostic(JSON.stringify({ fixture, source: sourceIdentity() }));
    environment.DTXWIKI_AUTO_BOOTSTRAP = String(autoBootstrap);
    const port = await findAvailablePort();
    function startEngine() {
      const child = spawn(python, [path.join(root, "desktop", "engine_entry.py"), "--port", String(port)], {
        env: environment, cwd: root, stdio: ["pipe", "pipe", "pipe"],
      });
      child.output = "";
      child.stdout.on("data", (chunk) => { child.output += chunk; });
      child.stderr.on("data", (chunk) => { child.output += chunk; });
      engines.push(child);
      return child;
    }
    const child = startEngine();
    const initPid = await until(() => {
      assert.equal(child.exitCode, null, child.output);
      assert.equal(child.signalCode, null, child.output);
      for (const pid of children(child.pid)) {
        initPids.add(pid);
        if (holdsDatabase(pid, database)) return pid;
      }
      return false;
    }, "init child database FD under acknowledged EXCLUSIVE lock");
    assert.ok(fs.readFileSync(`/proc/${initPid}/cmdline`, "utf8").includes("backend.app.runtime_init"));
    assert.equal(children(child.pid).length, 1, "worker must not start before initialization completes");
    const started = performance.now();
    await stopLocalEngine({ child, logPath: path.join(scratch, "engine.log") });
    assert.ok(child.exitCode !== null || child.signalCode !== null, child.output);
    assert.equal(alive(child.pid), false);
    assert.equal(alive(initPid), false, "init must be reaped before the API parent exits");
    t.diagnostic(JSON.stringify({ parent: child.pid, init: initPid, stopMs: Math.round(performance.now() - started), exit: child.exitCode, signal: child.signalCode }));
    holder.stdin.end();
    await reaped(holder);
    const restarted = startEngine();
    await until(async () => {
      assert.equal(restarted.exitCode, null, restarted.output);
      assert.equal(restarted.signalCode, null, restarted.output);
      try {
        const response = await fetch(`http://127.0.0.1:${port}/health/ready`, { signal: AbortSignal.timeout(500) });
        if (!response.ok) return false;
        const status = await response.json();
        return status.components.worker === "ok";
      } catch { return false; }
    }, "same-root and same-database restart with worker and HTTP ready");
    const workerPids = children(restarted.pid);
    assert.equal(workerPids.length, 1);
    await stopLocalEngine({ child: restarted, logPath: path.join(scratch, "restart.log") });
    assert.equal(restarted.exitCode, 0, restarted.output);
    for (const pid of workerPids) assert.equal(alive(pid), false);
  });
}
