#!/usr/bin/env node
/* global AbortSignal, Buffer, HTMLButtonElement, HTMLCanvasElement, MutationObserver, PerformanceObserver, URL, XMLHttpRequest, document, fetch, performance, requestAnimationFrame, setTimeout, window */

/**
 * Measure Graph View rendering in a real Chromium process.
 *
 * Managed mode builds the production SPA, starts the backend against a private
 * copy of a benchmark corpus, authenticates through the real REST API, and
 * drives the three graph surfaces.  External mode is useful for an already
 * running development server, but cannot provide the full cold-process gate.
 */

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createServer } from "node:net";
import {
  access,
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";
import { assertExact10kMeasurement, assertFreshNativeSceneProof, assertPc10kAcceptance, beginNativeSceneProof,
  finishNativeSceneProof, installGraphMeasurement, mapBenchmarkBudget, readCurrentGraphGpuMetadata,
  selectMapBenchmarkScale } from "./render_contract.mjs";
import { measureNodeInteractions, measureNormalMotion } from "./render_motion.mjs";
import { profileStableMotion, profileStartup, validateMotionProfileOptions, validateStartupProfileOptions } from "./render_profile.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_PATH);
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const TEMP_PREFIX = "dtxwiki-graph-render-";
const RESULT_SCHEMA = "dtxwiki.graph-render-benchmark.v1";
const DEFAULT_SURFACES = ["context-graph", "library-graph", "knowledge-map"];
const DEFAULT_CPU_RATES = [1, 4];
const GRAPH_BENCHMARK_PASSWORD = "graph-benchmark-only";
const KNOWLEDGE_40_EMAIL = "e2e-graph-owner@example.com";
const KNOWLEDGE_40_PASSWORD = "correct-password";

const SURFACE_DEFINITIONS = {
  "context-graph": {
    activationSelector: null,
    entityKey: "contextId",
    requestPath: "/graph/query",
    route(identity) {
      return `/workspaces/${encodeURIComponent(identity.workspaceId)}/context/pages/${encodeURIComponent(identity.contextId)}?tab=graph`;
    },
    readySelector:
      '#context-reader-panel-graph:not([hidden]) .graph-canvas-frame[data-renderer-status="ready"]',
  },
  "library-graph": {
    activationSelector: "#library-reader-tab-graph",
    entityKey: "libraryId",
    requestPath: "/graph/query",
    route(identity) {
      return `/workspaces/${encodeURIComponent(identity.workspaceId)}/library/items/${encodeURIComponent(identity.libraryId)}`;
    },
    readySelector:
      '#library-reader-panel-graph:not([hidden]) .graph-canvas-frame[data-renderer-status="ready"]',
  },
  "knowledge-map": {
    activationSelector: null,
    entityKey: null,
    requestPath: "/graph/overview/query",
    route(identity) {
      return `/workspaces/${encodeURIComponent(identity.workspaceId)}/graph`;
    },
    readySelector:
      '.workspace-graph .graph-canvas-frame[data-renderer-status="ready"]',
  },
};

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(helpText());
    return;
  }
  validateOptions(options);

  const startedAt = new Date().toISOString();
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), TEMP_PREFIX));
  let managedServer = null;
  let warmBrowser = null;
  let exitCode = 0;

  try {
    const git = gitMetadata();
    const harnessSha256 = await fileSha256(SCRIPT_PATH);
    const pythonVersion = runtimeVersion(options.python, ["--version"]);
    const corpus = await loadCorpusMetadata(options);
    const managed = !options.externalBaseUrl;
    const launch = managed
      ? await prepareManagedApplication(options, temporaryRoot, corpus)
      : {
          baseUrl: normalizeBaseUrl(options.externalBaseUrl),
          build_duration_ms: null,
          database: null,
          mode: "external",
          serverFactory: null,
        };

    if (managed) {
      managedServer = await launch.serverFactory.start();
    }
    const activeBaseUrl = launch.baseUrl;
    const principal = resolvePrincipal(options, corpus);

    warmBrowser = await launchChromium(options);
    const browserVersion = warmBrowser.version();
    const identity = await resolveBenchmarkIdentity({
      baseUrl: activeBaseUrl,
      browser: warmBrowser,
      contextId: options.contextId,
      email: principal.email,
      libraryId: options.libraryId,
      password: principal.password,
      workspaceId: options.workspaceId ?? corpus.workspace_id,
    });

    const samples = [];
    let gpu = null;
    for (const cpuRate of options.cpuRates) {
      for (const surface of options.surfaces) {
        for (let repetition = 1; repetition <= options.runs; repetition += 1) {
          const sample = await measureSurface({
            baseUrl: activeBaseUrl,
            browser: warmBrowser,
            cpuRate,
            identity,
            mode: "warm",
            options,
            principal,
            processesRestarted: [],
            repetition,
            surface,
          });
          samples.push(sample);
          gpu ??= sample.gpu ?? null;
          reportProgress(sample);
        }
      }
    }

    if (options.coldRuns > 0) {
      await warmBrowser.close();
      warmBrowser = null;
      if (managedServer) {
        await managedServer.stop();
        managedServer = null;
      }

      for (const cpuRate of options.cpuRates) {
        for (const surface of options.surfaces) {
          for (
            let repetition = 1;
            repetition <= options.coldRuns;
            repetition += 1
          ) {
            let coldServer = null;
            let coldBrowser = null;
            try {
              if (managed) coldServer = await launch.serverFactory.start();
              coldBrowser = await launchChromium(options);
              const sample = await measureSurface({
                baseUrl: activeBaseUrl,
                browser: coldBrowser,
                cpuRate,
                identity,
                mode: "cold",
                options,
                principal,
                processesRestarted: managed
                  ? ["backend", "chromium"]
                  : ["chromium"],
                repetition,
                surface,
              });
              samples.push(sample);
              gpu ??= sample.gpu ?? null;
              reportProgress(sample);
            } finally {
              if (coldBrowser) await coldBrowser.close();
              if (coldServer) await coldServer.stop();
            }
          }
        }
      }
    }

    const summaries = summarizeSamples(samples);
    const failures = samples.filter((sample) => sample.status === "error").length;
    const skips = samples.filter((sample) => sample.status === "skipped").length;
    const result = {
      schema: RESULT_SCHEMA,
      status: failures > 0 ? "failed" : skips > 0 ? "partial" : "complete",
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      metadata: {
        git,
        harness: {
          path: path.relative(REPOSITORY_ROOT, SCRIPT_PATH).replaceAll("\\", "/"),
          sha256: harnessSha256,
          measurement_helper_sha256: await fileSha256(path.join(SCRIPT_DIR, "render_contract.mjs")),
          motion_helper_sha256: await fileSha256(path.join(SCRIPT_DIR, "render_motion.mjs")),
          interaction_setup_helper_sha256: await fileSha256(path.join(SCRIPT_DIR, "render_interaction_setup.mjs")),
          profile_helper_sha256: await fileSha256(path.join(SCRIPT_DIR, "render_profile.mjs")),
        },
        host: hostMetadata(),
        runtimes: {
          browser: { engine: "chromium", version: browserVersion },
          node: process.version,
          playwright: playwrightVersion(),
          python: pythonVersion,
        },
        gpu,
        corpus: corpus.public,
        measurement_contract: {
          first_meaningful_render: {
            comparable_to_legacy_first_draw: false,
            render_boundary:
              "first requestAnimationFrame after data-renderer-status=ready",
            request_boundary: "first matching graph POST start after explicit requested-scale selection",
            ready_semantics:
              "ready is emitted after the current adapter layout settles; it is not a literal first-pixel draw",
          },
          detailed_timings: {
            decode: "response.json includes remaining body read, UTF-8 decode and JSON parse; not isolated parse CPU time",
            scene_upload: "JSON completion to first matching native visible draw, including scene construction, GPU submission and any Show unlinked UI action; not isolated GPU time",
            native_counts: "default-framebuffer POINTS vertices and instanced TRIANGLE_STRIP relationships; submission proof, not visible-pixel or non-overlap proof",
            interactive_frames: "Intervals between RAF boundaries containing actual full-scene native draws during recorded pan/zoom windows. Multiple native submissions in one RAF slot count once. This is a boundary proxy, not directly observed compositor presentation; raw native submission intervals are retained separately.",
          },
          gpu_identification: "The current graph canvas's already-observed native WebGL2 context, without creating another canvas or context. Hardware acceptance requires unmasked, non-generic, non-software identification; masked or historical separate-canvas metadata is not qualifying evidence.",
        },
        invocation: {
          actor: {
            authentication: "session",
            context_seed_id: identity.contextId,
            library_seed_id: identity.libraryId,
            name: principal.actor,
            workspace_id: identity.workspaceId,
          },
          concurrency: 1,
          cpu_throttling_rates: options.cpuRates,
          cold_gate_eligible:
            launch.mode === "managed" && options.coldRuns >= 20 && !options.cdpUrl,
          cold_processes:
            launch.mode === "managed"
              ? ["backend", "chromium"]
              : ["chromium"],
          cold_runs_per_surface_and_rate: options.coldRuns,
          headless: !options.headful,
          map_scale: options.mapScale,
          expected_map_relations: options.expectedMapRelations,
          chromium_attached: Boolean(options.cdpUrl),
          motion_seconds: options.motionSeconds,
          interaction_samples_per_kind: options.interactionSamples,
          pc_10k_acceptance: options.pc10kAcceptance,
          diagnostic_motion_profile: options.profileMotion,
          diagnostic_startup_profile: options.profileStartup,
          require_all_surfaces: options.requireAllSurfaces,
          runs_per_surface_and_rate: options.runs,
          smoke: options.smoke,
          surfaces: options.surfaces,
          viewport: options.viewport,
          zoom_frame_intervals: 60,
        },
        application: {
          base_url: activeBaseUrl,
          build_duration_ms: launch.build_duration_ms,
          mode: launch.mode,
          source_database: launch.database,
          static_identity: launch.static_identity ?? null,
        },
      },
      totals: {
        attempted: samples.length,
        completed: samples.length - failures - skips,
        errors: failures,
        failure_rate: ratio(failures, samples.length),
        skipped: skips,
        skip_rate: ratio(skips, samples.length),
      },
      summaries,
      samples,
    };

    await writeResult(options.out, result);
    process.stdout.write(
      `${JSON.stringify({
        errors: failures,
        output: path.resolve(options.out),
        skipped: skips,
        status: result.status,
      })}\n`,
    );
    if (failures > 0 || (options.requireAllSurfaces && skips > 0)) exitCode = 1;
  } finally {
    if (warmBrowser) await warmBrowser.close();
    if (managedServer) await managedServer.stop();
    if (options.keepTemp) {
      process.stderr.write(`[render-bench] kept temporary run at ${temporaryRoot}\n`);
    } else {
      await removeSafeTemporaryRoot(temporaryRoot);
    }
  }
  if (exitCode !== 0) process.exitCode = exitCode;
}

function parseArguments(argv) {
  const options = {
    actor: "owner",
    coldRuns: 20,
    cdpUrl: null,
    contextId: null,
    cpuRates: [...DEFAULT_CPU_RATES],
    db: null,
    email: null,
    externalBaseUrl: null,
    externalColdBrowserOnly: false,
    fixture: null,
    headful: false,
    help: false,
    keepTemp: false,
    libraryId: null,
    mapScale: "2k",
    expectedMapRelations: null,
    interactionSamples: 0,
    motionSeconds: 0,
    pc10kAcceptance: false,
    profileMotion: false,
    profileStartup: false,
    screenshotsDir: null,
    manifest: null,
    npm: process.platform === "win32" ? "npm.cmd" : "npm",
    out: null,
    password: null,
    port: 0,
    python: path.join(
      REPOSITORY_ROOT,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    ),
    requireAllSurfaces: false,
    runs: 20,
    smoke: false,
    surfaceTimeoutMs: 30_000,
    surfaces: [...DEFAULT_SURFACES],
    viewport: { height: 900, width: 1440 },
    workspaceId: null,
  };
  const valueOptions = new Map([
    ["--actor", "actor"],
    ["--cold-runs", "coldRuns"],
    ["--cdp-url", "cdpUrl"],
    ["--context-id", "contextId"],
    ["--cpu-rates", "cpuRates"],
    ["--db", "db"],
    ["--email", "email"],
    ["--external-base-url", "externalBaseUrl"],
    ["--fixture", "fixture"],
    ["--library-id", "libraryId"],
    ["--map-scale", "mapScale"],
    ["--expected-map-relations", "expectedMapRelations"],
    ["--interaction-samples", "interactionSamples"],
    ["--motion-seconds", "motionSeconds"],
    ["--screenshots-dir", "screenshotsDir"],
    ["--manifest", "manifest"],
    ["--npm", "npm"],
    ["--out", "out"],
    ["--password", "password"],
    ["--port", "port"],
    ["--python", "python"],
    ["--runs", "runs"],
    ["--surface-timeout-ms", "surfaceTimeoutMs"],
    ["--surfaces", "surfaces"],
    ["--viewport", "viewport"],
    ["--workspace-id", "workspaceId"],
  ]);

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") options.help = true;
    else if (argument === "--headful") options.headful = true;
    else if (argument === "--keep-temp") options.keepTemp = true;
    else if (argument === "--require-all-surfaces") options.requireAllSurfaces = true;
    else if (argument === "--external-cold-browser-only") {
      options.externalColdBrowserOnly = true;
    } else if (argument === "--smoke") options.smoke = true;
    else if (argument === "--pc-10k-acceptance") options.pc10kAcceptance = true;
    else if (argument === "--profile-motion") options.profileMotion = true;
    else if (argument === "--profile-startup") options.profileStartup = true;
    else if (valueOptions.has(argument)) {
      const value = argv[index + 1];
      if (value === undefined) throw new Error(`${argument} requires a value`);
      options[valueOptions.get(argument)] = value;
      index += 1;
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
  }

  options.runs = integerOption("--runs", options.runs, 0);
  options.coldRuns = integerOption("--cold-runs", options.coldRuns, 0);
  options.port = integerOption("--port", options.port, 0, 65_535);
  options.surfaceTimeoutMs = integerOption(
    "--surface-timeout-ms",
    options.surfaceTimeoutMs,
    1_000,
  );
  options.cpuRates = commaSeparatedIntegers("--cpu-rates", options.cpuRates, 1);
  options.surfaces = commaSeparatedValues(options.surfaces);
  options.viewport = parseViewport(options.viewport);
  options.motionSeconds = integerOption("--motion-seconds", options.motionSeconds, 0, 600);
  options.interactionSamples = integerOption("--interaction-samples", options.interactionSamples, 0, 100);
  mapBenchmarkBudget(options.mapScale);
  if (options.expectedMapRelations !== null) {
    options.expectedMapRelations = integerOption("--expected-map-relations", options.expectedMapRelations, 0, 100_000);
  }
  if (options.smoke) {
    options.runs = 1;
    options.coldRuns = 0;
  }
  if (options.profileMotion && options.motionSeconds === 0) options.motionSeconds = 5;
  return options;
}

function validateOptions(options) {
  if (!options.out) throw new Error("--out is required");
  validateMotionProfileOptions(options);
  validateStartupProfileOptions(options);
  if (options.cdpUrl) {
    const cdp = new URL(options.cdpUrl);
    if (!["http:", "https:", "ws:", "wss:"].includes(cdp.protocol)) throw new Error("--cdp-url requires an explicit HTTP or WebSocket endpoint");
    if (options.coldRuns > 0) throw new Error("--cdp-url cannot restart the attached browser; use --cold-runs 0");
  }
  if (options.mapScale === "10k" && !options.surfaces.includes("knowledge-map")) {
    throw new Error("--map-scale 10k requires the knowledge-map surface");
  }
  if (options.expectedMapRelations !== null && options.mapScale !== "10k") {
    throw new Error("--expected-map-relations requires --map-scale 10k");
  }
  if (options.db && options.fixture) {
    throw new Error("choose either --db or --fixture, not both");
  }
  if (!options.externalBaseUrl && !options.db && options.fixture !== "knowledge-40") {
    throw new Error("managed mode requires --db or --fixture knowledge-40");
  }
  if (options.externalBaseUrl && options.fixture) {
    throw new Error("--fixture cannot be combined with --external-base-url");
  }
  if (options.externalBaseUrl && !options.manifest) {
    throw new Error("external mode requires --manifest with corpus arguments");
  }
  if (
    options.externalBaseUrl &&
    options.coldRuns > 0 &&
    !options.externalColdBrowserOnly
  ) {
    throw new Error(
      "external mode cannot restart the backend for cold samples; set " +
        "--cold-runs 0 or explicitly use --external-cold-browser-only",
    );
  }
  if (!options.smoke && options.coldRuns > 0 && options.coldRuns < 20) {
    throw new Error("cold gates require at least 20 runs; use --smoke for one probe");
  }
  if (options.runs + options.coldRuns < 1) {
    throw new Error("at least one warm or cold run is required");
  }
  for (const surface of options.surfaces) {
    if (!(surface in SURFACE_DEFINITIONS)) {
      throw new Error(`unsupported surface ${surface}`);
    }
  }
  if (!options.surfaces.length) throw new Error("--surfaces cannot be empty");
  if (
    options.requireAllSurfaces &&
    DEFAULT_SURFACES.some((surface) => !options.surfaces.includes(surface))
  ) {
    throw new Error("--require-all-surfaces requires all three graph surfaces");
  }
  if (options.mapScale !== "10k" && !new Set(options.cpuRates).has(4)) {
    throw new Error("--cpu-rates must include the required 4x throttled condition");
  }
  if (!new Set(options.cpuRates).has(1)) {
    throw new Error("--cpu-rates must include the unthrottled 1x condition");
  }
  if (options.pc10kAcceptance && (options.mapScale !== "10k" || options.runs < 3
    || options.motionSeconds < 60 || options.interactionSamples < 10
    || options.surfaces.length !== 1 || options.surfaces[0] !== "knowledge-map" || options.smoke)) {
    throw new Error("--pc-10k-acceptance requires --map-scale 10k --surfaces knowledge-map --runs 3 (or more) --motion-seconds 60 (or more) --interaction-samples 10 (or more), without --smoke");
  }
}

async function loadCorpusMetadata(options) {
  if (options.fixture === "knowledge-40") {
    return {
      manifest: null,
      principal: { email: KNOWLEDGE_40_EMAIL, name: "owner" },
      public: {
        arguments: {
          documents: 40,
          profile: "fixture",
          relation_multiplier: 1,
          restricted_library_pct: 12.5,
          seed: "knowledge-40",
          workspaces: 1,
        },
        fixture: "knowledge-40",
        manifest: null,
      },
      workspace_id: null,
    };
  }

  let manifestPath = options.manifest;
  if (!manifestPath && options.db) manifestPath = `${options.db}.manifest.json`;
  if (!manifestPath) {
    return {
      manifest: null,
      principal: null,
      public: { arguments: null, fixture: null, manifest: null },
      workspace_id: null,
    };
  }
  const resolved = path.resolve(manifestPath);
  const parsed = JSON.parse(await readFile(resolved, "utf8"));
  if (
    !parsed.arguments ||
    typeof parsed.arguments !== "object" ||
    Array.isArray(parsed.arguments)
  ) {
    throw new Error(`benchmark manifest has no corpus arguments object: ${resolved}`);
  }
  const workspace = options.workspaceId
    ? parsed.workspaces?.find((candidate) => candidate.id?.toLowerCase() === options.workspaceId.toLowerCase())
    : parsed.workspaces?.[0] ?? null;
  if (options.workspaceId && !workspace) {
    throw new Error(`benchmark manifest does not name workspace ${options.workspaceId}`);
  }
  const principal = workspace?.principals?.find(
    (candidate) => candidate.name === options.actor,
  );
  return {
    manifest: parsed,
    principal: principal ?? null,
    public: {
      alembic_revision: parsed.application?.alembic_revision ?? parsed.alembic_revision ?? null,
      app_version: parsed.application?.version ?? parsed.app_version ?? null,
      arguments: parsed.arguments ?? null,
      content_sha256: parsed.database?.content_sha256 ?? null,
      fixture: null,
      generator_version: parsed.generator_version ?? null,
      manifest: path.basename(resolved),
      schema_sha256: parsed.database?.schema_sha256 ?? null,
      workspace_count: parsed.workspaces?.length ?? null,
    },
    workspace_id: workspace?.id ?? null,
  };
}

function resolvePrincipal(options, corpus) {
  const email = options.email ?? corpus.principal?.email;
  const password =
    options.password ??
    (options.fixture === "knowledge-40"
      ? KNOWLEDGE_40_PASSWORD
      : GRAPH_BENCHMARK_PASSWORD);
  if (!email) {
    throw new Error(
      `no ${options.actor} principal in the manifest; pass --email and --password`,
    );
  }
  return { actor: options.actor, email, password };
}

async function prepareManagedApplication(options, temporaryRoot, corpus) {
  await assertExecutable(options.python);
  const databasePath = path.join(temporaryRoot, "benchmark.db");
  let seedDurationMs = null;
  if (options.fixture === "knowledge-40") {
    const start = performance.now();
    await seedKnowledge40(options.python, databasePath);
    seedDurationMs = performance.now() - start;
  } else {
    const source = path.resolve(options.db);
    await assertReadableFile(source);
    for (const suffix of ["-wal", "-shm", "-journal"]) {
      if (await exists(`${source}${suffix}`)) {
        throw new Error(
          `refusing an uncheckpointed corpus with SQLite sidecar: ${source}${suffix}`,
        );
      }
    }
    await copyFile(source, databasePath, fsConstants.COPYFILE_EXCL);
  }

  const staticRoot = path.join(temporaryRoot, "spa");
  const buildStarted = performance.now();
  await runChecked(
    options.npm,
    [
      "--workspace",
      "frontend",
      "exec",
      "--",
      "vite",
      "build",
      "--outDir",
      staticRoot,
      "--emptyOutDir",
    ],
    {
      cwd: REPOSITORY_ROOT,
      env: {
        ...process.env,
        VITE_API_BASE_URL: "",
      },
      label: "frontend build",
    },
  );
  const buildDurationMs = performance.now() - buildStarted;
  const assetNames = (await readdir(path.join(staticRoot, "assets")))
    .filter((filename) => /^(?:index|cosmosAdapter|WorkspaceGraphScreen)-.*\.js$/u.test(filename)).sort();
  const staticIdentity = {
    index_sha256: await fileSha256(path.join(staticRoot, "index.html")),
    graph_assets: await Promise.all(assetNames.map(async (filename) => ({ filename,
      sha256: await fileSha256(path.join(staticRoot, "assets", filename)) }))),
  };
  process.stderr.write(`[render-bench] isolated SPA ready ${JSON.stringify(staticIdentity)}\n`);
  const port = options.port || (await availablePort());
  const baseUrl = `http://127.0.0.1:${port}`;
  const serverFactory = new ManagedServerFactory({
    baseUrl,
    databasePath,
    port,
    python: options.python,
    staticRoot,
    temporaryRoot,
  });
  corpus.public.seed_duration_ms = round(seedDurationMs);
  return {
    baseUrl,
    build_duration_ms: round(buildDurationMs),
    database: options.db ? path.basename(options.db) : "knowledge-40.fixture.db",
    mode: "managed",
    static_identity: staticIdentity,
    serverFactory,
  };
}

class ManagedServerFactory {
  constructor({ baseUrl, databasePath, port, python, staticRoot, temporaryRoot }) {
    this.baseUrl = baseUrl;
    this.databasePath = databasePath;
    this.port = port;
    this.python = python;
    this.staticRoot = staticRoot;
    this.temporaryRoot = temporaryRoot;
  }

  async start() {
    const databaseUrl = `sqlite+pysqlite:///${this.databasePath.replaceAll("\\", "/")}`;
    const serverScript = [
      "import uvicorn",
      "from backend.app.main import create_app",
      `uvicorn.run(create_app(bootstrap_on_startup=False), host="127.0.0.1", port=${this.port}, log_level="warning")`,
    ].join("; ");
    const child = spawn(this.python, ["-c", serverScript], {
      cwd: REPOSITORY_ROOT,
      env: {
        ...benchmarkPythonEnvironment(this.temporaryRoot),
        DTXWIKI_CORS_ORIGINS: this.baseUrl,
        DTXWIKI_DATABASE_URL: databaseUrl,
        DTXWIKI_LIBRARY_BLOB_ROOT: path.join(this.temporaryRoot, "library-blobs"),
        DTXWIKI_SESSION_COOKIE_SECURE: "false",
        DTXWIKI_SESSION_SECRET: "graph-render-benchmark-session-secret",
        DTXWIKI_STATIC_ROOT: this.staticRoot,
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    const output = boundedProcessOutput(child, 64 * 1024);
    child.stderr?.on("data", (chunk) => process.stderr.write(chunk));
    try {
      await waitForServer(this.baseUrl, child, output);
    } catch (error) {
      await stopProcess(child);
      throw error;
    }
    return {
      stop: async () => stopProcess(child),
    };
  }
}

async function seedKnowledge40(python, databasePath) {
  const databaseUrl = `sqlite+pysqlite:///${databasePath.replaceAll("\\", "/")}`;
  const script = `
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from backend.app.bootstrap import run_alembic_upgrade
from backend.app.db import make_engine
from backend.models.context import ContextPageVersion
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.credential_hashing import CredentialHasher
from backend.tests.utils.workspace_graph_corpora import build_knowledge_40, fixture_time, fixture_uuid
from backend.utils.content_hash import content_hash as calculate_content_hash

database_url = ${JSON.stringify(databaseUrl)}
run_alembic_upgrade(database_url)
engine = make_engine(database_url)
factory = sessionmaker(bind=engine, expire_on_commit=False)
with factory.begin() as session:
    corpus = build_knowledge_40(session)
    for version in session.scalars(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == corpus.workspace_id
        )
    ):
        version.content_hash = calculate_content_hash(version.body_md)
    owner_id = fixture_uuid("knowledge-40", "owner")
    owner = User(
        id=owner_id,
        email=${JSON.stringify(KNOWLEDGE_40_EMAIL)},
        email_verified_at=fixture_time(),
        password_hash=CredentialHasher().hash_password(${JSON.stringify(KNOWLEDGE_40_PASSWORD)}),
        display_name="Graph render benchmark owner",
        status="active",
        default_workspace_id=corpus.workspace_id,
        created_at=fixture_time(),
        updated_at=fixture_time(),
    )
    session.add(owner)
    session.flush()
    workspace = session.get(Workspace, corpus.workspace_id)
    workspace.owner_actor_id = owner_id
    workspace.owner_user_id = owner_id
    session.add(WorkspaceMembership(
        user_id=owner_id,
        workspace_id=corpus.workspace_id,
        role="owner",
        joined_at=fixture_time(),
    ))
engine.dispose()
`;
  await runChecked(python, ["-c", script], {
    cwd: REPOSITORY_ROOT,
    env: benchmarkPythonEnvironment(path.dirname(databasePath)),
    label: "knowledge-40 seed",
  });
}

function benchmarkPythonEnvironment(temporaryRoot) {
  // Managed runs own their application configuration and writable data. Keep
  // interpreter imports in this checkout even when the virtualenv is shared.
  return {
    ...Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("DTXWIKI_"))),
    PYTHONPATH: REPOSITORY_ROOT,
    DTXWIKI_DATA_DIR: path.join(temporaryRoot, "app-data"),
  };
}

async function resolveBenchmarkIdentity({
  baseUrl,
  browser,
  contextId,
  email,
  libraryId,
  password,
  workspaceId,
}) {
  const context = await browser.newContext();
  try {
    await authenticate(context, baseUrl, email, password);
    const workspacePayload = await apiJson(
      context,
      `${baseUrl}/api/v1/workspaces`,
    );
    const workspaceData = workspacePayload.data ?? workspacePayload;
    const resolvedWorkspaceId =
      workspaceId ??
      workspaceData.current_workspace_id ??
      workspaceData.default_workspace_id ??
      workspaceData.personal_workspace_id;
    if (!resolvedWorkspaceId) throw new Error("authenticated principal has no workspace");

    const resolvedContextId =
      contextId ??
      (await firstEntityId(
        context,
        `${baseUrl}/api/v1/workspaces/${resolvedWorkspaceId}/context/pages?status=published&limit=1`,
      ));
    const resolvedLibraryId =
      libraryId ??
      (await firstEntityId(
        context,
        `${baseUrl}/api/v1/workspaces/${resolvedWorkspaceId}/library/items?limit=1`,
      ));
    return {
      contextId: resolvedContextId,
      libraryId: resolvedLibraryId,
      workspaceId: resolvedWorkspaceId,
    };
  } finally {
    await context.close();
  }
}

async function firstEntityId(context, url) {
  const payload = await apiJson(context, url);
  const entries = Array.isArray(payload.data) ? payload.data : [];
  return entries[0]?.id ?? null;
}

async function authenticate(context, baseUrl, email, password) {
  const response = await context.request.post(`${baseUrl}/api/v1/auth/login`, {
    data: { email, password },
  });
  if (!response.ok()) {
    throw new Error(`login failed with HTTP ${response.status()}: ${await response.text()}`);
  }
}

async function apiJson(context, url) {
  const response = await context.request.get(url);
  if (!response.ok()) {
    throw new Error(`GET ${url} failed with HTTP ${response.status()}`);
  }
  return response.json();
}

async function launchChromium(options) {
  if (options.cdpUrl) return chromium.connectOverCDP(options.cdpUrl);
  return chromium.launch({
    headless: !options.headful,
    args: [
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
    ],
  });
}

async function measureSurface({
  baseUrl,
  browser,
  cpuRate,
  identity,
  mode,
  options,
  principal,
  processesRestarted,
  repetition,
  surface,
}) {
  const definition = SURFACE_DEFINITIONS[surface];
  const sample = {
    surface,
    mode,
    cpu_throttling_rate: cpuRate,
    repetition,
    concurrency: 1,
    console_errors: [],
    processes_restarted: processesRestarted,
    status: "error",
    error: null,
    first_meaningful_render_ms: null,
    gpu: null,
    graph_responses: [],
    http_errors: [],
    long_task_supported: null,
    long_tasks_over_100_ms: [],
    page_errors: [],
    diagnostic_error: null,
    renderer_diagnostics: null,
    map_measurement: null,
    graph_timing: null,
    motion: null,
    motion_profile: null,
    final_native_proof: null,
    interaction: null,
    screenshots: [],
    zoom_frame_interval_ms: null,
    zoom_frame_intervals_ms: [],
  };
  if (definition.entityKey && !identity[definition.entityKey]) {
    sample.status = "error";
    sample.error = `no visible ${definition.entityKey} is available for this actor`;
    return sample;
  }

  let context = null;
  let page = null;
  try {
    context = await browser.newContext({
      reducedMotion: "no-preference",
      viewport: options.viewport,
    });
    await authenticate(context, baseUrl, principal.email, principal.password);
    page = await context.newPage();
    page.setDefaultTimeout(options.surfaceTimeoutMs);
    page.setDefaultNavigationTimeout(options.surfaceTimeoutMs);
    await page.addInitScript(installGraphMeasurement);

    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: cpuRate });
    page.on("pageerror", (error) => sample.page_errors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") sample.console_errors.push(message.text());
    });
    page.on("response", (response) => {
      if (!response.ok()) {
        sample.http_errors.push({
          method: response.request().method(),
          status: response.status(),
          url_path: safeUrlPath(response.url()),
        });
      }
      if (!response.url().includes(definition.requestPath)) return;
      sample.graph_responses.push({
        ok: response.ok(),
        status: response.status(),
        url_path: safeUrlPath(response.url()),
      });
    });

    const route = definition.route(identity);
    const navigation = await page.goto(`${baseUrl}${route}`, {
      waitUntil: "domcontentloaded",
    });
    if (!navigation || !navigation.ok()) {
      throw new Error(
        `navigation to ${route} failed with HTTP ${navigation?.status() ?? "unknown"}`,
      );
    }
    if (definition.activationSelector) {
      const activation = page.locator(definition.activationSelector);
      try {
        await activation.waitFor({ state: "visible" });
        await activation.click();
      } catch (error) {
        throw new Error(
          `${surface} activation control ${definition.activationSelector} ` +
            `was unavailable: ${errorMessage(error)}`,
        );
      }
    }

    if (surface === "knowledge-map" && options.mapScale === "10k") {
      await page.locator(definition.readySelector).waitFor({ state: "visible" });
      const startup = async () => {
        await selectMapBenchmarkScale(page, options.mapScale, { startupDiagnostics: options.profileStartup });
        // The production default hides unlinked documents. Include them through
        // the real control so an apparently green 9,660-node scene is not 10K.
        await page.waitForFunction(() => window.__dtxGraphRenderBench.requests.some(
          (request) => request.budget?.documents === 10_000 && request.json_end_ms !== undefined,
        ));
        await page.locator('[data-map-node-count="10000"], .workspace-graph-orphan-toggle[aria-pressed="false"]')
          .first().waitFor({ state: "visible" });
        const unlinked = page.locator('.workspace-graph-orphan-toggle[aria-pressed="false"]');
        if (await unlinked.count()) {
          if (options.profileStartup) await page.evaluate(() => window.__dtxGraphRenderBench.markStartup("unlinked-click-call"));
          await unlinked.click();
          if (options.profileStartup) await page.evaluate(() => window.__dtxGraphRenderBench.markStartup("unlinked-click-resolved"));
        }
        await page.waitForFunction(() => {
          const state = window.__dtxGraphRenderBench;
          return state.firstCanvasFrameAt !== null && state.nativeDraws.node_vertices === 10_000;
        });
      };
      if (options.profileStartup) sample.startup_profile = await profileStartup({ page, cdp, runStartup: startup,
        outputPrefix: path.join(path.dirname(path.resolve(options.out)), `${surface}-${mode}-${cpuRate}x-${repetition}-startup`),
      });
      else await startup();
    }

    try {
      await page.locator(definition.readySelector).waitFor({ state: "visible" });
      await page
        .locator(`${definition.readySelector} canvas`)
        .first()
        .waitFor({ state: "visible" });
    } catch (error) {
      const diagnostics = await readInstrumentation(page, definition.requestPath);
      sample.renderer_diagnostics = await readRendererDiagnostics(page);
      const sawGraphRequest = diagnostics.requests.length > 0;
      if (!sawGraphRequest) {
        throw new Error(
          `${surface} did not expose its graph request and renderer within ` +
            `${options.surfaceTimeoutMs} ms; the route may be unavailable or disabled`,
        );
      }
      throw new Error(
        `${surface} issued a graph request but its renderer did not become ready: ${errorMessage(error)}`,
      );
    }

    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    const diagnostics = await readInstrumentation(page, definition.requestPath);
    if (diagnostics.firstCanvasFrameAt === null || diagnostics.requestStartAt === null) {
      throw new Error(
        `${surface} rendered without a measurable graph request-to-canvas boundary`,
      );
    }
    sample.first_meaningful_render_ms = round(
      diagnostics.firstCanvasFrameAt - diagnostics.requestStartAt,
    );
    sample.graph_timing = diagnostics.timing;
    if (surface === "knowledge-map") {
      const columns = await page.locator(`${definition.readySelector} [data-map-node-count]`).evaluate((element) => ({
        nodes: Number(element.getAttribute("data-map-node-count")),
        edges: Number(element.getAttribute("data-map-edge-count")),
      }));
      sample.map_measurement = {
        scale: options.mapScale,
        request: diagnostics.requestBudget,
        response: diagnostics.scene,
        columns,
        native: diagnostics.native,
        expectedRelations: options.expectedMapRelations,
      };
      if (options.mapScale === "10k") assertExact10kMeasurement(sample.map_measurement);
    }
    sample.gpu = await page.evaluate(readCurrentGraphGpuMetadata, definition.readySelector);

    if (surface === "knowledge-map" && options.motionSeconds > 0) {
      let motion;
      if (options.profileMotion) {
        const profiled = await profileStableMotion({ page, cdp,
          runMotion: () => measureNormalMotion(page, definition.readySelector, options.motionSeconds, { profileGate: true }),
          outputPrefix: path.join(path.dirname(path.resolve(options.out)),
            `${surface}-${mode}-${cpuRate}x-${repetition}-motion`),
        });
        motion = profiled.motion;
        sample.motion_profile = profiled.diagnostics;
      } else motion = await measureNormalMotion(page, definition.readySelector, options.motionSeconds);
      sample.motion = {
        ...motion,
        initial_draw_frame_interval_ms: metricSummary(intervalDifferences(motion.initial_native_timestamps)),
        stable_draw_frame_interval_ms: metricSummary(intervalDifferences(motion.stable_native_timestamps)),
        interactive_draw_frame_interval_ms: metricSummary(motion.interactive_native_intervals),
        interactive_raf_frame_interval_ms: metricSummary(motion.interactive_raf_intervals),
      };
      await page.getByRole("button", { name: "Fit graph", exact: true }).click();
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      if (options.screenshotsDir) sample.screenshots.push(await captureSample(page, options, sample, "normal-motion"));
    }
    if (surface === "knowledge-map" && options.interactionSamples > 0) {
      const samples = await measureNodeInteractions(page, definition.readySelector, options.interactionSamples);
      sample.interaction = { samples };
      for (const kind of ["hover", "selection"]) {
        const matching = samples.filter((entry) => entry.kind === kind);
        sample.interaction[kind] = {
          attempted: matching.length,
          missed: matching.filter((entry) => entry.status !== "hit").length,
          latency_ms: metricSummary(matching.filter((entry) => entry.status === "hit").map((entry) => entry.latency_ms)),
        };
      }
    }
    if (options.screenshotsDir) sample.screenshots.push(await captureSample(page, options, sample, "interactions"));

    if (surface === "knowledge-map" && options.mapScale === "10k") await beginNativeSceneProof(page);
    const frameIntervals = await measureZoomFrames(
      page,
      definition.readySelector,
      60,
    );
    sample.zoom_frame_intervals_ms = frameIntervals.map((value) => round(value));
    sample.zoom_frame_interval_ms = metricSummary(frameIntervals);
    if (surface === "knowledge-map" && options.mapScale === "10k") {
      sample.final_native_proof = await finishNativeSceneProof(page);
      assertFreshNativeSceneProof(sample.final_native_proof, diagnostics.scene.nodes, diagnostics.scene.edges);
    }
    await page.waitForTimeout(0);
    const finalDiagnostics = await readInstrumentation(page, definition.requestPath);
    sample.long_task_supported = finalDiagnostics.longTaskSupported;
    sample.long_tasks_over_100_ms = finalDiagnostics.longTasks.map((entry) => ({
      duration_ms: round(entry.duration_ms),
      name: entry.name,
      start_ms: round(entry.start_ms),
    }));

    const failedResponse = sample.graph_responses.find((response) => !response.ok);
    if (failedResponse) {
      throw new Error(
        `${surface} graph request returned HTTP ${failedResponse.status}`,
      );
    }
    if (sample.page_errors.length) {
      throw new Error(`${surface} raised ${sample.page_errors.length} page error(s)`);
    }
    if (sample.console_errors.length) {
      throw new Error(`${surface} logged ${sample.console_errors.length} console error(s)`);
    }
    if (sample.http_errors.length) {
      throw new Error(`${surface} received ${sample.http_errors.length} HTTP error response(s)`);
    }
    if (options.pc10kAcceptance) assertPc10kAcceptance(sample);
    sample.status = "complete";
  } catch (error) {
    sample.status = "error";
    sample.error = errorMessage(error);
    if (page && !page.isClosed()) {
      try {
        await collectFailureDiagnostics(sample, page, definition.requestPath);
      } catch (diagnosticError) {
        sample.diagnostic_error = errorMessage(diagnosticError);
      }
    }
  } finally {
    if (page) await page.close();
    if (context) await context.close();
  }
  return sample;
}

function intervalDifferences(timestamps) {
  return timestamps.slice(1).map((timestamp, index) => timestamp - timestamps[index]);
}

async function captureSample(page, options, sample, phase) {
  const directory = path.resolve(options.screenshotsDir);
  await mkdir(directory, { recursive: true });
  const filename = `${sample.surface}-${sample.mode}-${sample.cpu_throttling_rate}x-${sample.repetition}-${phase}.png`;
  const destination = path.join(directory, filename);
  if (await exists(destination)) throw new Error(`refusing to replace screenshot: ${destination}`);
  await page.screenshot({ path: destination });
  return destination;
}

async function readInstrumentation(page, requestPath) {
  return page.evaluate((pathFragment) => {
    const state = window.__dtxGraphRenderBench;
    const requests = state.requests.filter(
      (request) =>
        request.method === "POST" && request.url.includes(pathFragment),
    );
    const eligible = requests.filter(
      (request) =>
        state.firstCanvasFrameAt === null ||
        request.start_ms <= state.firstCanvasFrameAt,
    );
    const requestStartAt = eligible.length
      ? Math.min(...eligible.map((request) => request.start_ms))
      : null;
    const matched = eligible[0] ?? null;
    const scene = matched?.scene;
    const resource = performance.getEntriesByType("resource").find((entry) =>
      entry.name === new URL(matched?.url ?? "", window.location.href).href
      && entry.startTime >= (matched?.start_ms ?? 0) - 1);
    const delta = (end, start) => Number.isFinite(end) && Number.isFinite(start) ? end - start : null;
    return {
      firstCanvasFrameAt: state.firstCanvasFrameAt,
      longTaskSupported: state.longTaskSupported,
      longTasks: state.longTasks,
      requests: requests.map(({ method, start_ms, url, budget }) => ({ method, start_ms, url, budget })),
      requestStartAt,
      requestBudget: matched?.budget ?? null,
      scene: scene?.nodes && scene?.edges ? {
        nodes: scene.nodes.length,
        edges: scene.edges.length,
        documents: scene.nodes.filter((node) => node.grain === "document").length,
        clusters: scene.nodes.filter((node) => node.grain === "cluster").length,
        aggregated_edges: scene.edges.filter((edge) => edge.aggregate).length,
        candidate_documents: scene.counts?.candidate_documents,
        candidate_relations: scene.counts?.candidate_relations,
        representation: scene.representation,
        completeness: scene.completeness,
        truncated: scene.truncated,
      } : null,
      native: state.nativeDraws,
      timing: {
        request_to_headers_ms: delta(matched?.headers_at_ms, matched?.start_ms),
        resource_response_body_ms: delta(resource?.responseEnd, resource?.responseStart),
        body_read_decode_json_ms: delta(matched?.json_end_ms, matched?.json_start_ms),
        json_to_native_scene_draw_ms: delta(state.nativeDraws.first_scene_draw_at, matched?.json_end_ms),
        response_end_to_first_frame_ms: delta(state.firstCanvasFrameAt, resource?.responseEnd),
        json_to_first_frame_ms: delta(state.firstCanvasFrameAt, matched?.json_end_ms),
        encoded_body_bytes: resource?.encodedBodySize ?? null,
        decoded_body_bytes: resource?.decodedBodySize ?? null,
      },
    };
  }, requestPath);
}

async function collectFailureDiagnostics(sample, page, requestPath) {
  const diagnostics = await readInstrumentation(page, requestPath);
  sample.long_task_supported = diagnostics.longTaskSupported;
  sample.long_tasks_over_100_ms = diagnostics.longTasks.map((entry) => ({
    duration_ms: round(entry.duration_ms),
    name: entry.name,
    start_ms: round(entry.start_ms),
  }));
  sample.renderer_diagnostics ??= await readRendererDiagnostics(page);
  if (
    sample.first_meaningful_render_ms === null &&
    diagnostics.firstCanvasFrameAt !== null &&
    diagnostics.requestStartAt !== null
  ) {
    sample.first_meaningful_render_ms = round(
      diagnostics.firstCanvasFrameAt - diagnostics.requestStartAt,
    );
  }
}

async function readRendererDiagnostics(page) {
  return page.evaluate(() => ({
    frames: Array.from(document.querySelectorAll(".graph-canvas-frame")).map(
      (frame) => ({
        status: frame.getAttribute("data-renderer-status"),
        visible: Boolean(frame.getClientRects().length),
      }),
    ),
    graph_panels: Array.from(document.querySelectorAll('[id$="panel-graph"]')).map(
      (panel) => ({
        hidden: panel.hasAttribute("hidden"),
        id: panel.id,
        text: panel.textContent?.trim().slice(0, 240) ?? "",
      }),
    ),
    location: window.location.href,
  }));
}

async function measureZoomFrames(page, readySelector, intervals) {
  return page.evaluate(
    ({ frameCount, selector }) => {
      const frame = document.querySelector(selector);
      const toolbar = frame?.parentElement?.querySelector(".graph-canvas-toolbar") ??
        document.querySelector(".graph-canvas-toolbar");
      const buttons = Array.from(toolbar?.querySelectorAll("button") ?? []);
      const zoomIn = buttons.find((button) => {
        const label = (button.getAttribute("aria-label") ?? "").toLowerCase();
        return label.includes("zoom in") || label.includes("확대");
      });
      const zoomOut = buttons.find((button) => {
        const label = (button.getAttribute("aria-label") ?? "").toLowerCase();
        return label.includes("zoom out") || label.includes("축소");
      });
      if (!(zoomIn instanceof HTMLButtonElement)) {
        throw new Error("zoom-in control is unavailable");
      }
      if (!(zoomOut instanceof HTMLButtonElement)) {
        throw new Error("zoom-out control is unavailable");
      }
      return new Promise((resolve) => {
        const values = [];
        let previous = null;
        const next = (timestamp) => {
          if (previous !== null) values.push(timestamp - previous);
          if (values.length >= frameCount) {
            resolve(values);
            return;
          }
          (values.length % 2 === 0 ? zoomIn : zoomOut).click();
          previous = timestamp;
          requestAnimationFrame(next);
        };
        requestAnimationFrame(next);
      });
    },
    { frameCount: intervals, selector: readySelector },
  );
}

function summarizeSamples(samples) {
  const groups = new Map();
  for (const sample of samples) {
    const key = `${sample.surface}\0${sample.mode}\0${sample.cpu_throttling_rate}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(sample);
  }
  return [...groups.values()].map((group) => {
    const complete = group.filter((sample) => sample.status === "complete");
    const errors = group.filter((sample) => sample.status === "error");
    const skipped = group.filter((sample) => sample.status === "skipped");
    const firstRender = complete.map((sample) => sample.first_meaningful_render_ms);
    const zoom = complete.flatMap((sample) => sample.zoom_frame_intervals_ms);
    return {
      surface: group[0].surface,
      mode: group[0].mode,
      cpu_throttling_rate: group[0].cpu_throttling_rate,
      requested: group.length,
      completed: complete.length,
      errors: errors.length,
      failure_rate: ratio(errors.length, group.length),
      skipped: skipped.length,
      skip_rate: ratio(skipped.length, group.length),
      first_meaningful_render_ms: metricSummary(firstRender),
      zoom_frame_interval_ms: metricSummary(zoom),
      response_end_to_first_frame_ms: metricSummary(complete.map((sample) => sample.graph_timing?.response_end_to_first_frame_ms)),
      body_read_decode_json_ms: metricSummary(complete.map((sample) => sample.graph_timing?.body_read_decode_json_ms)),
      json_to_native_scene_draw_ms: metricSummary(complete.map((sample) => sample.graph_timing?.json_to_native_scene_draw_ms)),
      long_tasks_over_100_ms: complete.reduce(
        (total, sample) => total + sample.long_tasks_over_100_ms.length,
        0,
      ),
    };
  });
}

function metricSummary(values) {
  const finite = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (!finite.length) return null;
  return {
    count: finite.length,
    max: round(finite.at(-1)),
    p50: round(nearestRank(finite, 0.5)),
    p95: round(nearestRank(finite, 0.95)),
  };
}

function nearestRank(sorted, percentile) {
  const index = Math.max(0, Math.ceil(percentile * sorted.length) - 1);
  return sorted[index];
}

function reportProgress(sample) {
  const metric =
    sample.status === "complete"
      ? `first=${sample.first_meaningful_render_ms}ms zoom-p95=${sample.zoom_frame_interval_ms?.p95}ms`
      : sample.error;
  process.stderr.write(
    `[render-bench] ${sample.mode} ${sample.cpu_throttling_rate}x ` +
      `${sample.surface} #${sample.repetition}: ${sample.status} ${metric ?? ""}\n`,
  );
}

async function writeResult(output, result) {
  const resolved = path.resolve(output);
  await mkdir(path.dirname(resolved), { recursive: true });
  await writeFile(resolved, `${JSON.stringify(result, null, 2)}\n`, {
    encoding: "utf8",
    flag: "wx",
  });
}

async function fileSha256(filename) {
  return createHash("sha256").update(await readFile(filename)).digest("hex");
}

function gitMetadata() {
  const commit = runText("git", ["rev-parse", "HEAD"], REPOSITORY_ROOT);
  const status = runText(
    "git",
    ["status", "--porcelain=v1", "--untracked-files=all"],
    REPOSITORY_ROOT,
  );
  return {
    commit,
    dirty: status.length > 0,
    dirty_entry_count: status ? status.split(/\r?\n/u).length : 0,
  };
}

function hostMetadata() {
  const cpus = os.cpus();
  return {
    arch: os.arch(),
    cpu_count: cpus.length,
    cpu_model: cpus[0]?.model ?? null,
    cpu_speed_mhz: cpus[0]?.speed ?? null,
    memory_bytes: os.totalmem(),
    os_platform: os.platform(),
    os_release: os.release(),
    os_type: os.type(),
  };
}

function playwrightVersion() {
  try {
    const packagePath = path.join(
      REPOSITORY_ROOT,
      "node_modules",
      "@playwright",
      "test",
      "package.json",
    );
    const result = spawnSync(process.execPath, ["-e", `console.log(require(${JSON.stringify(packagePath)}).version)`], {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
    });
    return result.status === 0 ? result.stdout.trim() : null;
  } catch {
    return null;
  }
}

function runtimeVersion(command, args) {
  const result = spawnSync(command, args, {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) return `unavailable: ${result.error.message}`;
  return `${result.stdout ?? ""}${result.stderr ?? ""}`.trim();
}

function runText(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed: ${result.error?.message ?? result.stderr}`,
    );
  }
  return result.stdout.trim();
}

async function runChecked(command, args, { cwd, env, label }) {
  const child = spawnPortable(command, args, {
    cwd,
    env,
    stdio: "inherit",
    windowsHide: true,
  });
  const status = await waitForExit(child);
  if (status !== 0) throw new Error(`${label} failed with exit code ${status}`);
}

function spawnPortable(command, args, options) {
  if (process.platform !== "win32" || !/\.(?:cmd|bat)$/iu.test(command)) {
    return spawn(command, args, options);
  }
  const commandLine = [command, ...args].map(escapeWindowsArgument).join(" ");
  return spawn("cmd.exe", ["/d", "/s", "/c", commandLine], {
    ...options,
    windowsVerbatimArguments: true,
  });
}

function escapeWindowsArgument(value) {
  const text = String(value);
  if (text.length > 0 && !/[%!"\s&|<>()^]/u.test(text)) return text;
  let escaped = "";
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character !== "\\") {
      escaped += character === '"' ? '\\"' : character;
      continue;
    }
    let runLength = 0;
    while (index < text.length && text[index] === "\\") {
      runLength += 1;
      index += 1;
    }
    if (index < text.length && text[index] === '"') {
      escaped += "\\".repeat(runLength * 2 + 1) + '\\"';
    } else if (index === text.length) {
      escaped += "\\".repeat(runLength * 2);
      index -= 1;
    } else {
      escaped += "\\".repeat(runLength) + text[index];
    }
  }
  return `"${escaped.replace(/([%!])/gu, "^$1")}"`;
}

function boundedProcessOutput(child, limit) {
  const chunks = [];
  let size = 0;
  const append = (chunk) => {
    const text = chunk.toString();
    chunks.push(text);
    size += Buffer.byteLength(text);
    while (size > limit && chunks.length > 1) {
      size -= Buffer.byteLength(chunks.shift());
    }
  };
  child.stdout?.on("data", append);
  child.stderr?.on("data", append);
  return () => chunks.join("").slice(-limit);
}

async function waitForServer(baseUrl, child, output) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(
        `backend exited with code ${child.exitCode} before readiness\n${output()}`,
      );
    }
    try {
      const response = await fetch(`${baseUrl}/health/live`, {
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return;
    } catch {
      // The process is still starting.
    }
    await delay(100);
  }
  throw new Error(`backend did not become live within 30 seconds\n${output()}`);
}

async function stopProcess(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  const graceful = await Promise.race([
    waitForExit(child).then(() => true),
    delay(5_000).then(() => false),
  ]);
  if (!graceful && child.exitCode === null) {
    child.kill("SIGKILL");
    await waitForExit(child);
  }
}

function waitForExit(child) {
  if (child.exitCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
}

async function availablePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((error) => {
        if (error) reject(error);
        else if (port === null) reject(new Error("failed to allocate a local port"));
        else resolve(port);
      });
    });
  });
}

async function assertExecutable(executable) {
  await access(path.resolve(executable), fsConstants.X_OK).catch((error) => {
    throw new Error(`Python executable is unavailable: ${executable}: ${error.message}`);
  });
}

async function assertReadableFile(filename) {
  const info = await stat(filename);
  if (!info.isFile()) throw new Error(`not a file: ${filename}`);
  await access(filename, fsConstants.R_OK);
}

async function exists(filename) {
  try {
    await access(filename, fsConstants.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function removeSafeTemporaryRoot(candidate) {
  const resolved = path.resolve(candidate);
  const relative = path.relative(path.resolve(os.tmpdir()), resolved);
  if (
    !relative ||
    path.dirname(relative) !== "." ||
    !path.basename(relative).startsWith(TEMP_PREFIX)
  ) {
    throw new Error(`refusing to remove unsafe temporary path: ${resolved}`);
  }
  await rm(resolved, { force: true, maxRetries: 10, recursive: true, retryDelay: 100 });
}

function integerOption(name, value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return parsed;
}

function commaSeparatedIntegers(name, value, minimum) {
  const entries = commaSeparatedValues(value).map((entry) =>
    integerOption(name, entry, minimum),
  );
  return [...new Set(entries)];
}

function commaSeparatedValues(value) {
  const entries = Array.isArray(value) ? value : String(value).split(",");
  return entries.map((entry) => String(entry).trim()).filter(Boolean);
}

function parseViewport(value) {
  if (typeof value === "object") return value;
  const match = /^(\d+)x(\d+)$/u.exec(value);
  if (!match) throw new Error("--viewport must use WIDTHxHEIGHT, for example 1440x900");
  return {
    height: integerOption("--viewport height", match[2], 320, 8_192),
    width: integerOption("--viewport width", match[1], 320, 8_192),
  };
}

function normalizeBaseUrl(value) {
  const parsed = new URL(value);
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error("--external-base-url must be HTTP or HTTPS");
  }
  return parsed.href.replace(/\/$/u, "");
}

function safeUrlPath(value) {
  try {
    return new URL(value).pathname;
  } catch {
    return String(value);
  }
}

function ratio(numerator, denominator) {
  return denominator ? round(numerator / denominator, 6) : 0;
}

function round(value, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  const multiplier = 10 ** digits;
  return Math.round(value * multiplier) / multiplier;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function helpText() {
  return `Usage:
  node scripts/bench/graph/render_bench.mjs --db CORPUS.db --out RESULT.json [options]
  node scripts/bench/graph/render_bench.mjs --fixture knowledge-40 --smoke --out RESULT.json

Managed application options:
  --db PATH                    deterministic corpus built by build_corpus.py
  --manifest PATH              defaults to <db>.manifest.json; required externally
  --fixture knowledge-40       build the focused 40-document smoke fixture
  --port N                     backend port; 0 chooses an available port (default)
  --python PATH                Python executable (default: repository .venv)
  --npm PATH                   npm executable

Measurement options:
  --surfaces LIST              comma-separated context-graph,library-graph,knowledge-map
  --runs N                     warm runs per surface/rate (default: 20)
  --cold-runs N                cold runs per surface/rate (default: 20)
  --cpu-rates LIST             must include 1 and 4 (default: 1,4)
  --surface-timeout-ms N       surface readiness timeout (default: 30000)
  --viewport WIDTHxHEIGHT      browser viewport (default: 1440x900)
  --map-scale 2k|10k           explicit PC Knowledge Map UI choice (default: 2k)
  --expected-map-relations N   require this exact complete 10K corpus relation count
  --cdp-url URL                attach only to the explicitly provided Chromium CDP endpoint
  --motion-seconds N           first 5s separately, then N seconds of native draws/Zoom
  --interaction-samples N      one native hover/selection attempt per distinct GPU-backed node
  --pc-10k-acceptance           require hardware GPU, 3 runs, 60s motion, 10 hits/kind and budgets
  --profile-motion             diagnostic-only CPU/readPixels profile of 5 stable seconds; never acceptance
  --profile-startup            diagnostic-only CPU/control phases before explicit10K through full first frame
  --screenshots-dir PATH       save viewport evidence; existing images are never replaced
  --actor NAME                 manifest principal (default: owner)
  --context-id ID              override the first visible Context page
  --library-id ID              override the first visible Library item
  --workspace-id ID            override the authenticated current workspace
  --email EMAIL                override manifest principal email
  --password PASSWORD          override local benchmark password; never recorded
  --headful                    show Chromium
  --require-all-surfaces       return non-zero when a surface is unavailable
  --smoke                      one warm run and no cold run; not a performance gate
  --keep-temp                  retain the verified temporary run directory

External application options:
  --external-base-url URL      use an already running app; skips build/server launch
  --external-cold-browser-only allow cold samples that restart Chromium but not server

Output:
  --out PATH                   new JSON result path; existing files are never replaced
  --help                       show this text
`;
}

main().catch((error) => {
  process.stderr.write(`[render-bench] ${errorMessage(error)}\n`);
  process.exitCode = 1;
});
