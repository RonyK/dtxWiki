import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

export const THIRD_PARTY_NOTICES_FILE = "THIRD_PARTY_NOTICES.txt";

// The repository root, which vendored file paths in the registry are relative to.
export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const LEGAL_FILE = /^(?:licen[cs]e|copying|notice)(?:[.-].*)?$/i;

const MIT_PERMISSION = `Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`;

type LegalFile = { name: string; text: string };

type BundledPackage = {
  name: string;
  version: string;
  license: string;
  root: string;
  legalFiles: LegalFile[];
};

// Third-party code the package scan below cannot attribute, because no
// package root holds its license text. Two kinds:
//   - `file`: a file copied into this repository. It is always listed, its
//     SHA-256 pins the copy the note describes, and NOTICE records the same
//     license text for the source distribution.
//   - `package`: code that npm package compiles into a file it ships, with the
//     license text kept below the package root. It is listed whenever that
//     package is bundled.
// The build fails if a registered file is missing or changed, or if a license
// text is missing or does not read as `license`.
export type VendoredComponent = {
  name: string;
  license: string;
  source: string;
  note: string;
} & (
  | { file: string; sha256: string; licenseText: string }
  | { package: string; licenseFile: string }
);

const KARPATHY_SKILLS = "https://github.com/multica-ai/andrej-karpathy-skills";
const KARPATHY_TEMPLATE = "frontend/src/features/agent/templates/karpathy-agent-guidelines.md";

// pdf.js compiles the JavaScript glue of its WebAssembly colour-management
// (QCMS) and JPEG 2000 (OpenJPEG) decoders into build/pdf.worker.min.mjs, which
// the bundle ships as the PDF.js worker, and keeps the license texts under
// wasm/. The .wasm binaries themselves are not bundled.
const PDFJS_WORKER_NOTE =
  "pdf.js compiles this component's JavaScript glue into the PDF.js worker " +
  "(build/pdf.worker.min.mjs) that the web UI ships.";

export const VENDORED_COMPONENTS: readonly VendoredComponent[] = [
  {
    name: "andrej-karpathy-skills CLAUDE.md",
    license: "MIT",
    source: `${KARPATHY_SKILLS}/blob/8462496b34419f20b32778610571ac723e91f94c/CLAUDE.md`,
    note:
      `Copied unmodified to ${KARPATHY_TEMPLATE} and offered as an agent page template. ` +
      "The project, formerly forrestchang/andrej-karpathy-skills, declares the MIT License " +
      "in its README and plugin manifest but ships no license file or copyright line, so " +
      "this notice names its contributors.",
    file: KARPATHY_TEMPLATE,
    sha256: "694a2d721e41c385f3db492838c23299826df5ba9809e3b0721aac70021e196a",
    licenseText:
      "MIT License\n\n" +
      `Copyright (c) andrej-karpathy-skills contributors (${KARPATHY_SKILLS})\n\n` +
      MIT_PERMISSION,
  },
  {
    name: "QCMS",
    license: "MIT",
    source: "https://github.com/FirefoxGraphics/qcms",
    note: PDFJS_WORKER_NOTE,
    package: "pdfjs-dist",
    licenseFile: "wasm/LICENSE_QCMS",
  },
  // Labelled from the text pdfjs-dist ships for it, although pdf.js's
  // external/qcms/README.md calls this wrapper MIT; the shipped text is what
  // is reproduced.
  {
    name: "pdf.js.qcms",
    license: "BSD-2-Clause",
    source: "https://github.com/mozilla/pdf.js.qcms",
    note: PDFJS_WORKER_NOTE,
    package: "pdfjs-dist",
    licenseFile: "wasm/LICENSE_PDFJS_QCMS",
  },
  {
    name: "OpenJPEG",
    license: "BSD-2-Clause",
    source: "https://github.com/uclouvain/openjpeg",
    note: PDFJS_WORKER_NOTE,
    package: "pdfjs-dist",
    licenseFile: "wasm/LICENSE_OPENJPEG",
  },
  {
    name: "pdf.js.openjpeg",
    license: "BSD-2-Clause",
    source: "https://github.com/mozilla/pdf.js.openjpeg",
    note: PDFJS_WORKER_NOTE,
    package: "pdfjs-dist",
    licenseFile: "wasm/LICENSE_PDFJS_OPENJPEG",
  },
];

// Maps a module id or asset source path to the root of the npm package that
// provides it, or null for first-party code. Vite writes module ids with "/"
// on every platform while path.resolve yields "\\" on Windows, where the
// desktop release builds, so both are normalized before matching.
export function packageRoot(file: string): string | null {
  const clean = file.replace(/^\0/, "").split("?")[0].replaceAll("\\", "/");
  const marker = "/node_modules/";
  const index = clean.lastIndexOf(marker);
  if (index === -1) {
    return null;
  }
  const rest = clean.slice(index + marker.length).split("/");
  const depth = rest[0].startsWith("@") ? 2 : 1;
  return clean.slice(0, index + marker.length) + rest.slice(0, depth).join("/");
}

function licenseOf(manifest: Record<string, unknown>): string {
  const { license, licenses } = manifest;
  if (typeof license === "string") {
    return license;
  }
  if (license && typeof license === "object" && "type" in license) {
    return String((license as { type: unknown }).type);
  }
  if (Array.isArray(licenses)) {
    return licenses
      .map((entry) => (entry && typeof entry === "object" ? String(entry.type) : String(entry)))
      .join(" OR ");
  }
  return "UNKNOWN";
}

// A package that declares no license in package.json but ships one of the
// fixed-wording license texts is labelled from that text (and marked as such)
// instead of UNKNOWN. Order matters: BSD-3-Clause is BSD-2-Clause plus the
// endorsement clause. The text is reproduced in the notices either way.
const LICENSE_TEXT_MARKERS: readonly (readonly [RegExp, string])[] = [
  [/Permission is hereby granted, free of charge, to any person obtaining a copy/i, "MIT"],
  [
    /Permission to use, copy, modify, and\/or distribute this software for any purpose with or without fee is hereby granted/i,
    "ISC",
  ],
  [/Apache License Version 2\.0/i, "Apache-2.0"],
  [/Redistribution and use in source and binary forms.*Neither the name/i, "BSD-3-Clause"],
  [/Redistribution and use in source and binary forms/i, "BSD-2-Clause"],
];

export function inferLicense(text: string): string | null {
  const flattened = text.replace(/\s+/g, " ");
  for (const [marker, identifier] of LICENSE_TEXT_MARKERS) {
    if (marker.test(flattened)) {
      return identifier;
    }
  }
  return null;
}

function authorOf(manifest: Record<string, unknown>): string | null {
  const { author } = manifest;
  if (typeof author === "string" && author.trim()) {
    return author.trim();
  }
  if (author && typeof author === "object" && "name" in author) {
    const { name, email } = author as { name?: unknown; email?: unknown };
    return email ? `${String(name)} <${String(email)}>` : String(name);
  }
  return null;
}

// Some MIT packages publish without their repository's LICENSE file. The MIT
// terms are fixed, so the notice is rebuilt from the package's own author
// field and marked as such; no copyright year is invented.
function reconstructedMit(manifest: Record<string, unknown>) {
  const author = authorOf(manifest);
  if (licenseOf(manifest) !== "MIT" || author === null) {
    return [];
  }
  return [
    {
      name: "LICENSE (reconstructed from package.json; not shipped in the package)",
      text: `MIT License\n\nCopyright (c) ${author}\n\n${MIT_PERMISSION}`,
    },
  ];
}

function readPackage(root: string): BundledPackage {
  const manifest = JSON.parse(readFileSync(path.join(root, "package.json"), "utf-8"));
  const legalFiles = readdirSync(root)
    .filter((name) => LEGAL_FILE.test(name))
    .sort()
    .map((name) => ({ name, text: readFileSync(path.join(root, name), "utf-8").trim() }));
  const declared = licenseOf(manifest);
  const inferred =
    declared === "UNKNOWN"
      ? (legalFiles.map((file) => inferLicense(file.text)).find((id) => id !== null) ?? null)
      : null;
  return {
    name: String(manifest.name),
    version: String(manifest.version),
    license:
      inferred === null
        ? declared
        : `${inferred} (from its license text; package.json declares none)`,
    root,
    legalFiles: legalFiles.length > 0 ? legalFiles : reconstructedMit(manifest),
  };
}

type VendoredNotice = {
  title: string;
  license: string;
  source: string;
  note: string;
  legalFile: LegalFile;
};

// Resolves the registry against the bundled packages. Returns every problem
// rather than stopping at the first, so one failed build names them all.
function resolveVendored(
  registry: readonly VendoredComponent[],
  packages: readonly BundledPackage[],
  repoRoot: string,
): { notices: VendoredNotice[]; problems: string[] } {
  const notices: VendoredNotice[] = [];
  const problems: string[] = [];
  const add = (component: VendoredComponent, title: string, legalFile: LegalFile) => {
    if (inferLicense(legalFile.text) !== component.license) {
      problems.push(`${title}: ${legalFile.name} is not ${component.license} license text`);
      return;
    }
    const { license, source, note } = component;
    notices.push({ title, license, source, note, legalFile });
  };
  for (const component of registry) {
    if ("file" in component) {
      const file = path.join(repoRoot, component.file);
      if (!existsSync(file)) {
        problems.push(`${component.file} is registered but missing`);
        continue;
      }
      const digest = createHash("sha256").update(readFileSync(file)).digest("hex");
      if (digest !== component.sha256) {
        problems.push(
          `${component.file} no longer matches its registered copy (sha256 ${digest}); ` +
            "update its entry and NOTICE",
        );
        continue;
      }
      add(component, component.name, { name: "License", text: component.licenseText.trim() });
      continue;
    }
    for (const pkg of packages.filter((candidate) => candidate.name === component.package)) {
      const title = `${component.name}, in ${pkg.name}@${pkg.version}`;
      const file = path.join(pkg.root, component.licenseFile);
      if (!existsSync(file)) {
        problems.push(`${title}: the package no longer ships ${component.licenseFile}`);
        continue;
      }
      add(component, title, {
        name: `${component.package}/${component.licenseFile}`,
        text: readFileSync(file, "utf-8").trim(),
      });
    }
  }
  return { notices, problems };
}

function render(packages: BundledPackage[], vendored: VendoredNotice[]): string {
  const header = [
    "Third-party software bundled with the dtxWiki web UI",
    "",
    "The dtxWiki web UI includes the following third-party packages. Each is",
    "licensed under its own terms, reproduced below as shipped by the package.",
    "Each package can be obtained from the npm registry (https://www.npmjs.com/)",
    "under the name and version shown.",
    "",
    ...packages.map((pkg) => `- ${pkg.name}@${pkg.version}: ${pkg.license}`),
    ...(vendored.length === 0
      ? []
      : [
          "",
          "It also includes the following third-party components, which the package",
          "license texts above do not cover: files copied into the dtxWiki source",
          "tree, and code a package compiles into a file it ships.",
          "",
          ...vendored.map((notice) => `- ${notice.title}: ${notice.license}`),
        ]),
  ].join("\n");
  const sections = packages.map((pkg) =>
    [
      "=".repeat(78),
      `${pkg.name}@${pkg.version}`,
      `License: ${pkg.license}`,
      "=".repeat(78),
      ...pkg.legalFiles.flatMap((file) => ["", `--- ${file.name} ---`, "", file.text]),
    ].join("\n"),
  );
  const vendoredSections = vendored.map((notice) =>
    [
      "=".repeat(78),
      notice.title,
      `License: ${notice.license}`,
      `Source: ${notice.source}`,
      "=".repeat(78),
      "",
      notice.note,
      "",
      `--- ${notice.legalFile.name} ---`,
      "",
      notice.legalFile.text,
    ].join("\n"),
  );
  return `${[header, ...sections, ...vendoredSections].join("\n\n")}\n`;
}

// Collects every npm package that contributes code or assets to the bundle,
// including assets referenced by URL (the PDF.js worker, KaTeX fonts) that
// never enter the module graph, and emits their license texts and notices,
// followed by the vendored components registered above. The build fails when
// a bundled package ships no license text or a registered component cannot be
// resolved, because the notice file would otherwise under-report what the
// bundle redistributes. The options exist for tests.
export function thirdPartyNotices({
  vendored = VENDORED_COMPONENTS,
  repoRoot = REPO_ROOT,
}: { vendored?: readonly VendoredComponent[]; repoRoot?: string } = {}): Plugin {
  return {
    name: "dtxwiki-third-party-notices",
    apply: "build",
    generateBundle(_options, bundle) {
      const sources = new Set<string>(this.getModuleIds());
      for (const output of Object.values(bundle)) {
        if (output.type === "asset") {
          for (const name of output.originalFileNames) {
            sources.add(path.resolve(name));
          }
        }
      }
      const roots = new Set<string>();
      for (const source of sources) {
        const root = packageRoot(source);
        if (root && existsSync(path.join(root, "package.json"))) {
          roots.add(root);
        }
      }
      const byId = new Map<string, BundledPackage>();
      for (const root of roots) {
        const pkg = readPackage(root);
        byId.set(`${pkg.name}@${pkg.version}`, pkg);
      }
      const packages = [...byId.values()].sort((a, b) =>
        a.name === b.name ? a.version.localeCompare(b.version) : a.name.localeCompare(b.name),
      );
      // An empty list means the scan found nothing, not that the bundle is
      // first-party only; React alone rules that out.
      if (packages.length === 0) {
        this.error("found no bundled npm packages; the node_modules scan is broken");
      }
      const missing = packages.filter((pkg) => pkg.legalFiles.length === 0);
      if (missing.length > 0) {
        this.error(
          `bundled packages ship no license text: ${missing
            .map((pkg) => `${pkg.name}@${pkg.version} (${pkg.license})`)
            .join(", ")}`,
        );
      }
      const { notices, problems } = resolveVendored(vendored, packages, repoRoot);
      if (problems.length > 0) {
        this.error(`vendored third-party components are not attributable: ${problems.join("; ")}`);
      }
      this.emitFile({
        type: "asset",
        fileName: THIRD_PARTY_NOTICES_FILE,
        source: render(packages, notices),
      });
    },
  };
}
