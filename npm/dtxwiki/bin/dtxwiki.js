#!/usr/bin/env node
// dtxWiki is a Python application published on PyPI. This launcher runs the
// PyPI release with the same version as this npm package, through uv's `uvx`
// or, when uv is not installed, through `pipx run`, and passes the arguments,
// the exit status and termination signals through unchanged.
"use strict";

const { spawn } = require("node:child_process");
const os = require("node:os");
const { version } = require("../package.json");

const SPEC = `dtxwiki==${version}`;

// uv finds or downloads Python 3.12, the version dtxWiki is built and tested
// on. pipx uses its own default interpreter.
const RUNNERS = [
  { command: "uvx", prefix: ["--python", "3.12", "--from", SPEC, "dtxwiki"] },
  { command: "pipx", prefix: ["run", "--spec", SPEC, "dtxwiki"] },
];

// Tells dtxwiki that this launcher, not a package manager it could drive,
// decides which version runs, so `dtxwiki update` prints the npm steps.
const LAUNCHER_ENV = { DTXWIKI_LAUNCHER: "npm" };

const NO_RUNNER = [
  `dtxwiki: @dtxsuite/dtxwiki runs dtxWiki ${version} from PyPI with uv or pipx, and neither is on PATH.`,
  "Install uv (https://docs.astral.sh/uv/getting-started/installation/) and run this command again,",
  `or install dtxWiki without npm: pipx install ${SPEC}`,
].join("\n");

// A terminal sends Ctrl+C (SIGINT) and hang-up (SIGHUP) to the whole
// foreground process group, so the runner already gets them; sending them
// again would look like a second Ctrl+C and cut dtxWiki's graceful shutdown
// short. This process only waits for the runner. SIGTERM comes from `kill` or
// a service manager and reaches this process alone, so it is passed on.
function relaySignals(child) {
  process.on("SIGINT", () => {});
  process.on("SIGHUP", () => {});
  process.on("SIGTERM", () => child.kill("SIGTERM"));
}

function exitLike(code, signal) {
  if (signal === null) {
    process.exit(code ?? 1);
  }
  // Die of the same signal so the shell reports it as the runner's status.
  process.exitCode = 128 + (os.constants.signals[signal] ?? 0);
  process.removeAllListeners(signal);
  process.kill(process.pid, signal);
}

function start(index) {
  const runner = RUNNERS[index];
  if (runner === undefined) {
    process.stderr.write(`${NO_RUNNER}\n`);
    process.exit(1);
  }
  const child = spawn(runner.command, [...runner.prefix, ...process.argv.slice(2)], {
    stdio: "inherit",
    env: { ...process.env, ...LAUNCHER_ENV },
  });
  let spawned = false;
  child.on("spawn", () => {
    spawned = true;
    relaySignals(child);
  });
  child.on("error", (error) => {
    if (!spawned && error.code === "ENOENT") {
      start(index + 1);
      return;
    }
    process.stderr.write(`dtxwiki: could not run ${runner.command}: ${error.message}\n`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    if (spawned) {
      exitLike(code, signal);
    }
  });
}

start(0);
