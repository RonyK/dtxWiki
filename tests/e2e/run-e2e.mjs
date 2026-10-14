import { mkdtempSync, rmSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

import {
  buildWindowsCommandLine,
  resolveSafeE2ETempDir,
} from "./run-e2e.helpers.mjs";

const isWindows = process.platform === "win32";
const pythonExecutable = path.join(
  ".venv",
  isWindows ? "Scripts" : "bin",
  isWindows ? "python.exe" : "python",
);
const npxExecutable = isWindows ? "npx.cmd" : "npx";
const npmExecutable = isWindows ? "npm.cmd" : "npm";
// One list decides which browser projects Playwright runs AND which per-project
// fixtures prepare_backend_db.py seeds. They used to be two hardcoded copies of
// the same three names, so changing one silently left the other seeding or
// expecting accounts nobody would use.
//
// standard   -- Chromium runs everything; Firefox and WebKit run only the specs
//               tagged @cross-browser, which are the ones whose contract is an
//               engine behavior rather than our own code.
// exhaustive -- every spec on all three engines. This is what the weekly
//               workflow runs; it is not a pull-request gate.
const E2E_PROFILE = process.env.DTXWIKI_E2E_PROFILE ?? "standard";
if (E2E_PROFILE !== "standard" && E2E_PROFILE !== "exhaustive") {
  throw new Error(
    `DTXWIKI_E2E_PROFILE must be "standard" or "exhaustive", got ${E2E_PROFILE}`,
  );
}
process.env.DTXWIKI_E2E_PROFILE = E2E_PROFILE;

const e2eDir = mkdtempSync(path.join(tmpdir(), "dtxwiki-e2e-"));
const dbPath = path.join(e2eDir, "dtxwiki-e2e.db");
const dbUrl = `sqlite+pysqlite:///${dbPath.replace(/\\/g, "/")}`;
const libraryBlobRoot = path.join(e2eDir, "library-blobs");
const oauthJwtKeyPath = path.join(e2eDir, "oauth-jwt.pem");
const spaBuildDir = path.join(e2eDir, "spa");
const playwrightArgs = process.argv.slice(2);

if (!isWindows) {
  const mode = statSync(e2eDir).mode & 0o777;
  if (mode !== 0o700) {
    throw new Error(
      `e2e tmp dir ${e2eDir} has unsafe mode ${mode.toString(8)}; ` +
        "expected 0700. Refusing to run e2e in a shared-host environment.",
    );
  }
}

process.env.DTXWIKI_E2E_DATABASE_PATH = dbPath;
process.env.DTXWIKI_E2E_DATABASE_URL = dbUrl;
process.env.DTXWIKI_E2E_LIBRARY_BLOB_ROOT = libraryBlobRoot;
process.env.DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH = oauthJwtKeyPath;
process.env.DTXWIKI_E2E_STATIC_ROOT = spaBuildDir;
// Pin it rather than inherit it: an operator with VITE_API_BASE_URL exported
// would otherwise get that absolute origin baked into the bundle, which puts a
// second origin back in front of the browser. "" makes every SPA request
// origin-relative (frontend/src/shared/api/client.ts falls through to the
// packaged default in a production build).
process.env.VITE_API_BASE_URL = "";
// The Workspace Knowledge Map needs no build flag: /graph asks the backend,
// which answers from the workspace owner's Settings switch
// (prepare_backend_db.py seeds it on for the graph corpus).

let exitCode = 1;
let cleanupError;
try {
  exitCode = run(pythonExecutable, [
    "-m",
    "backend.cli.main",
    "keygen",
    "--output",
    oauthJwtKeyPath,
  ]);
  if (exitCode === 0) {
    // `-m` and not the script path. Running a script puts the SCRIPT's
    // directory on sys.path, not the working directory, so `import backend`
    // fell through to whatever the editable install's finder resolves -- which
    // can be a different worktree. The backend webServer starts with
    // `python -c`, where sys.path[0] is the working directory, so the two
    // halves of this harness were free to load different copies of the
    // application: the seeder migrated one checkout's schema and the server
    // then refused it as an unknown revision.
    exitCode = run(pythonExecutable, ["-m", "tests.e2e.prepare_backend_db"]);
  }
  if (exitCode === 0) {
    // The browser must see the one origin the wheel ships: the backend serving
    // this bundle plus the API. Running a Vite dev server on a second port
    // instead gave every page two socket pools, so host network churn surfaced
    // as ERR_NETWORK_CHANGED failures in specs that were testing something else.
    //
    // `vite build`, not `npm --workspace frontend run build`: that script
    // prefixes `tsc --noEmit`, which is the frontend job's work and would add
    // minutes to every e2e invocation. --emptyOutDir because outDir is outside
    // the Vite project root, where Vite otherwise refuses to clear it.
    exitCode = run(npmExecutable, [
      "--workspace",
      "frontend",
      "exec",
      "--",
      "vite",
      "build",
      "--outDir",
      spaBuildDir,
      "--emptyOutDir",
    ]);
  }
  if (exitCode === 0) {
    exitCode = run(npxExecutable, ["playwright", "test", ...playwrightArgs]);
  }
} finally {
  // Playwright has stopped its webServer children before the CLI returns, so
  // the per-run database and immutable Library source blobs can be removed as
  // one unit. Retries cover short-lived Windows file-handle release delays.
  const cleanupTarget = resolveSafeE2ETempDir(e2eDir, tmpdir());
  try {
    rmSync(cleanupTarget, {
      force: true,
      maxRetries: 10,
      recursive: true,
      retryDelay: 100,
    });
  } catch (error) {
    cleanupError = error;
    process.stderr.write(
      `[e2e cleanup] Failed to remove verified temporary directory ` +
        `${cleanupTarget} after Windows handle retries. Remove this exact ` +
        `directory manually. ${error instanceof Error ? error.message : String(error)}\n`,
    );
  }
}

if (cleanupError && exitCode === 0) {
  throw cleanupError;
}
if (exitCode !== 0) {
  process.exit(exitCode);
}

function run(command, args) {
  // Windows .cmd/.bat (e.g. npx.cmd) requires shell-spawned invocation since
  // Node v22 closed CVE-2024-27980 by rejecting batch files under shell:false.
  // POSIX keeps shell:false for direct execve. On Windows we explicitly invoke
  // cmd.exe with the canonical /d /s /c flags so we control quoting fully and
  // avoid Node's built-in `shell:true` join, which uses POSIX-style escaping.
  if (isWindows) {
    const commandLine = buildWindowsCommandLine(command, args);
    const result = spawnSync("cmd.exe", ["/d", "/s", "/c", commandLine], {
      env: process.env,
      stdio: "inherit",
      windowsVerbatimArguments: true,
    });
    if (result.error) {
      throw result.error;
    }
    return result.status ?? 1;
  }
  const result = spawnSync(command, args, {
    env: process.env,
    shell: false,
    stdio: "inherit",
  });
  if (result.error) {
    throw result.error;
  }
  return result.status ?? 1;
}

// Fix-6 Task 3.3 (M-F5-CODEX-3): the previous helper used POSIX backslash
// escaping. cmd.exe treats backslash as a literal character; only the MSVCRT
// argument parser of the target program (Playwright -> Node) interprets it.
// Escape logic lives in ./run-e2e.helpers.mjs so the escape algorithm can be
// unit-tested by tests/e2e/run-e2e.test.mjs without spawning subprocesses.
