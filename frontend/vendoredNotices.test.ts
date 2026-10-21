import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  packageRoot,
  REPO_ROOT,
  thirdPartyNotices,
  VENDORED_COMPONENTS,
  type VendoredComponent,
} from "./thirdPartyNotices";

const resolveModule = createRequire(import.meta.url).resolve;
const REACT_ENTRY = resolveModule("react");
const PDFJS_ROOT = packageRoot(resolveModule("pdfjs-dist"))!;
const PDFJS_VERSION: string = JSON.parse(
  readFileSync(path.join(PDFJS_ROOT, "package.json"), "utf-8"),
).version;
const PDFJS_WORKER = path.join(PDFJS_ROOT, "build", "pdf.worker.min.mjs");

const TEMPLATES_DIR = "frontend/src/features/agent/templates";
// Agent templates written for dtxWiki. Every other file in the templates
// directory must be registered in VENDORED_COMPONENTS, so a template copied
// from elsewhere cannot ship without its notice.
const FIRST_PARTY_TEMPLATES: readonly string[] = [];

const VENDORED_FILES = VENDORED_COMPONENTS.flatMap((component) =>
  "file" in component ? [component] : [],
);

// Runs the plugin's generateBundle the way Rollup does and returns the notices
// it emits; `this.error` throws, as it aborts the build.
function emitNotices(
  { moduleIds = [REACT_ENTRY], assets = [] as string[] } = {},
  options?: Parameters<typeof thirdPartyNotices>[0],
): string {
  let emitted: string | undefined;
  const context = {
    getModuleIds: () => moduleIds.values(),
    emitFile: (file: { source?: unknown }) => {
      emitted = String(file.source);
      return "notices";
    },
    error: (message: string) => {
      throw new Error(message);
    },
  };
  const bundle = Object.fromEntries(
    assets.map((asset) => [path.basename(asset), { type: "asset", originalFileNames: [asset] }]),
  );
  const generateBundle = thirdPartyNotices(options).generateBundle as unknown as (
    this: typeof context,
    outputOptions: unknown,
    outputBundle: Record<string, unknown>,
  ) => void;
  generateBundle.call(context, {}, bundle);
  if (emitted === undefined) {
    throw new Error("the plugin emitted no notices");
  }
  return emitted;
}

function sectionOf(notices: string, title: string): string {
  const start = notices.indexOf(`\n${title}\nLicense: `);
  expect(start, `no section titled ${title}`).toBeGreaterThan(-1);
  const end = notices.indexOf(`\n${"=".repeat(78)}\n`, notices.indexOf("=".repeat(78), start) + 78);
  return notices.slice(start, end === -1 ? undefined : end);
}

const flat = (text: string) => text.replace(/\s+/g, " ").trim();

describe("pinned upstream package notices", () => {
  const licenseFile = "frontend/licenses/storybook-10.5.3.LICENSE";
  const licenseText = readFileSync(path.join(REPO_ROOT, licenseFile), "utf-8").replaceAll("\r\n", "\n");
  const source =
    "https://github.com/storybookjs/storybook/blob/9ac273930a49ad33b6a331f1f36dc472f5c36054/LICENSE";
  const packages = ["storybook", "@storybook/react", "@storybook/react-dom-shim"];
  let scratch: string | undefined;

  afterEach(() => {
    if (scratch) rmSync(scratch, { recursive: true, force: true });
    scratch = undefined;
  });

  function packageFixture(overrides: Record<string, unknown> = {}) {
    scratch = mkdtempSync(path.join(tmpdir(), "dtxwiki-upstream-license-"));
    const root = path.join(scratch, "node_modules", "storybook");
    mkdirSync(root, { recursive: true });
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({
        name: "storybook",
        version: "10.5.3",
        license: "MIT",
        repository: { url: "https://github.com/storybookjs/storybook.git" },
        ...overrides,
      }),
    );
    const copiedLicense = path.join(scratch, licenseFile);
    mkdirSync(path.dirname(copiedLicense), { recursive: true });
    writeFileSync(copiedLicense, licenseText);
    return {
      root,
      copiedLicense,
      emit: () =>
        emitNotices(
          { moduleIds: [path.join(root, "index.js")] },
          { vendored: [], repoRoot: scratch },
        ),
    };
  }

  it.each(packages)("attributes the installed %s package to its exact upstream license", (name) => {
    const module = resolveModule(`${name}/package.json`);
    const notices = emitNotices({ moduleIds: [module] }, { vendored: [] });
    const section = sectionOf(notices, `${name}@10.5.3`);
    expect(section).toContain(`from ${source}; omitted by the npm package`);
    expect(section).toContain(licenseText.trim());
    expect(section).toContain("Copyright (c) 2024 Storybook");
    expect(section).toContain("NON-INFRINGEMENT");
    expect(section).not.toContain("reconstructed from package.json");
  });

  it("preserves the npm package's own license when it ships one", () => {
    const fixture = packageFixture();
    writeFileSync(path.join(fixture.root, "LICENSE"), "A license shipped by the package.");
    rmSync(fixture.copiedLicense);
    expect(fixture.emit()).toContain("A license shipped by the package.");
    expect(fixture.emit()).not.toContain(source);
  });

  it.each([
    { name: "unregistered-package" },
    { version: "10.5.4" },
  ])("rejects missing legal text for an unregistered package or version: %j", (metadata) => {
    expect(packageFixture(metadata).emit).toThrow("bundled packages ship no license text");
  });

  it.each([
    { license: "Apache-2.0" },
    { repository: { url: "https://example.com/unrelated.git" } },
  ])("rejects changed upstream metadata: %j", (metadata) => {
    expect(packageFixture(metadata).emit).toThrow("metadata does not match its pinned upstream license");
  });

  it("rejects a missing copy of the pinned license", () => {
    const fixture = packageFixture();
    rmSync(fixture.copiedLicense);
    expect(fixture.emit).toThrow("pinned upstream license is missing");
  });

  it("rejects modified upstream license text, even when it still reads as MIT", () => {
    const fixture = packageFixture();
    writeFileSync(fixture.copiedLicense, licenseText.replace("2024 Storybook", "2026 Someone"));
    expect(fixture.emit).toThrow("pinned upstream license changed");
  });

  it("accepts the same pinned license in a Windows checkout", () => {
    const fixture = packageFixture();
    writeFileSync(fixture.copiedLicense, licenseText.replaceAll("\n", "\r\n"));
    expect(fixture.emit()).toContain(licenseText.trim());
  });

  it("does not add Storybook notices when it is absent from the bundle", () => {
    const notices = emitNotices({}, { vendored: [] });
    expect(notices).not.toContain("Storybook");
    expect(notices).not.toContain("storybook");
  });
});

describe("vendored third-party notices", () => {
  it("emits the copied agent template's MIT notice and the PDF.js worker's embedded licenses", () => {
    const notices = emitNotices({ assets: [PDFJS_WORKER] });

    expect(notices).toContain("- andrej-karpathy-skills CLAUDE.md: MIT\n");
    const template = sectionOf(notices, "andrej-karpathy-skills CLAUDE.md");
    expect(template).toContain(
      "Source: https://github.com/multica-ai/andrej-karpathy-skills/blob/8462496b34419f20b32778610571ac723e91f94c/CLAUDE.md",
    );
    expect(template).toContain(`Copied unmodified to ${TEMPLATES_DIR}/karpathy-agent-guidelines.md`);
    expect(template).toContain(
      "Copyright (c) andrej-karpathy-skills contributors (https://github.com/multica-ai/andrej-karpathy-skills)",
    );
    expect(template).toContain("Permission is hereby granted, free of charge");
    expect(template).toContain('THE SOFTWARE IS PROVIDED "AS IS"');

    for (const [name, license, file] of [
      ["QCMS", "MIT", "LICENSE_QCMS"],
      ["pdf.js.qcms", "BSD-2-Clause", "LICENSE_PDFJS_QCMS"],
      ["OpenJPEG", "BSD-2-Clause", "LICENSE_OPENJPEG"],
      ["pdf.js.openjpeg", "BSD-2-Clause", "LICENSE_PDFJS_OPENJPEG"],
    ]) {
      const title = `${name}, in pdfjs-dist@${PDFJS_VERSION}`;
      expect(notices).toContain(`- ${title}: ${license}\n`);
      const section = sectionOf(notices, title);
      expect(section).toContain(`--- pdfjs-dist/wasm/${file} ---`);
      expect(section).toContain(readFileSync(path.join(PDFJS_ROOT, "wasm", file), "utf-8").trim());
    }
  });

  it("tells readers where to obtain each listed package", () => {
    expect(emitNotices()).toContain("npm registry (https://www.npmjs.com/)");
  });

  it("lists code embedded in a package only when that package is bundled", () => {
    const notices = emitNotices();

    expect(notices).not.toContain("pdfjs-dist");
    expect(notices).not.toContain("QCMS");
    expect(notices).toContain("- andrej-karpathy-skills CLAUDE.md: MIT\n");
  });

  it("records each copied file in NOTICE with the license text the notices emit", () => {
    const notice = flat(readFileSync(path.join(REPO_ROOT, "NOTICE"), "utf-8"));

    expect(VENDORED_FILES.length).toBeGreaterThan(0);
    for (const component of VENDORED_FILES) {
      expect(notice).toContain(component.file);
      expect(notice).toContain(flat(component.licenseText));
    }
  });

  it("accounts for every agent template as first-party or registered third-party material", () => {
    const registered = new Set(VENDORED_FILES.map((component) => component.file));
    const unaccounted = readdirSync(path.join(REPO_ROOT, TEMPLATES_DIR))
      .map((name) => `${TEMPLATES_DIR}/${name}`)
      .filter((file) => !registered.has(file) && !FIRST_PARTY_TEMPLATES.includes(file));

    expect(unaccounted).toEqual([]);
  });
});

describe("vendored notices fail the build when they cannot be attributed", () => {
  let scratch: string;

  afterEach(() => {
    rmSync(scratch, { recursive: true, force: true });
  });

  function vendoredFile(content: string, overrides: Partial<VendoredComponent> = {}) {
    scratch = mkdtempSync(path.join(tmpdir(), "dtxwiki-vendored-"));
    mkdirSync(path.join(scratch, "vendor"));
    writeFileSync(path.join(scratch, "vendor", "copied.md"), content);
    return {
      name: "copied",
      license: "MIT",
      source: "https://example.com/copied",
      note: "Copied for a test.",
      file: "vendor/copied.md",
      sha256: createHash("sha256").update(content).digest("hex"),
      licenseText: VENDORED_FILES[0].licenseText,
      ...overrides,
    } as VendoredComponent;
  }

  it("accepts a registered file that matches its entry", () => {
    const component = vendoredFile("upstream text\n");

    expect(emitNotices({}, { vendored: [component], repoRoot: scratch })).toContain(
      "- copied: MIT\n",
    );
  });

  it("rejects a registered file that is missing", () => {
    const component = vendoredFile("upstream text\n");
    rmSync(path.join(scratch, "vendor", "copied.md"));

    expect(() => emitNotices({}, { vendored: [component], repoRoot: scratch })).toThrow(
      "vendor/copied.md is registered but missing",
    );
  });

  it("rejects a registered file that no longer matches the copy its entry describes", () => {
    const component = vendoredFile("upstream text\n");
    writeFileSync(path.join(scratch, "vendor", "copied.md"), "locally edited text\n");

    expect(() => emitNotices({}, { vendored: [component], repoRoot: scratch })).toThrow(
      "vendor/copied.md no longer matches its registered copy",
    );
  });

  it("rejects an entry without license text", () => {
    const component = vendoredFile("upstream text\n", { licenseText: "Copyright (c) Someone" });

    expect(() => emitNotices({}, { vendored: [component], repoRoot: scratch })).toThrow(
      "copied: License is not MIT license text",
    );
  });

  it("rejects license text that does not match the declared license", () => {
    const component = vendoredFile("upstream text\n", { license: "BSD-2-Clause" });

    expect(() => emitNotices({}, { vendored: [component], repoRoot: scratch })).toThrow(
      "copied: License is not BSD-2-Clause license text",
    );
  });

  // A bundled package "host" whose index.js embeds a component licensed in
  // wasm/LICENSE_EMBEDDED, which is written only when `withLicense` is set.
  function embeddingPackage(withLicense: boolean) {
    scratch = mkdtempSync(path.join(tmpdir(), "dtxwiki-vendored-"));
    const root = path.join(scratch, "node_modules", "host");
    mkdirSync(path.join(root, "wasm"), { recursive: true });
    writeFileSync(path.join(root, "package.json"), '{"name":"host","version":"1.0.0","license":"MIT"}');
    writeFileSync(path.join(root, "LICENSE"), VENDORED_FILES[0].licenseText);
    writeFileSync(path.join(root, "index.js"), "export {};\n");
    if (withLicense) {
      writeFileSync(path.join(root, "wasm", "LICENSE_EMBEDDED"), VENDORED_FILES[0].licenseText);
    }
    const embedded: VendoredComponent = {
      name: "embedded",
      license: "MIT",
      source: "https://example.com/embedded",
      note: "Compiled into host.",
      package: "host",
      licenseFile: "wasm/LICENSE_EMBEDDED",
    };
    return () =>
      emitNotices(
        { moduleIds: [path.join(root, "index.js")] },
        { vendored: [embedded], repoRoot: scratch },
      );
  }

  it("accepts a bundled package that ships the registered license file", () => {
    expect(embeddingPackage(true)()).toContain("- embedded, in host@1.0.0: MIT\n");
  });

  it("rejects a bundled package that no longer ships a registered license file", () => {
    expect(embeddingPackage(false)).toThrow(
      "embedded, in host@1.0.0: the package no longer ships wasm/LICENSE_EMBEDDED",
    );
  });
});
