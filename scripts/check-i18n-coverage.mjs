#!/usr/bin/env node

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Resolve relative to this file, not process.cwd() — the script must work
// whether invoked as `node scripts/check-i18n-coverage.mjs` from the repo
// root (root package.json) or `node ../scripts/check-i18n-coverage.mjs`
// from frontend/ (frontend package.json runs its scripts with cwd set to
// frontend/, not the repo root).
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// Overridable via env for the fixture tests in
// tests/static/test_i18n_coverage_gate.py, which point these at an
// isolated temp directory instead of the real (and, in this repo, actively
// multi-agent-edited) frontend/src tree.
const LOCALE_ROOT = process.env.I18N_CHECK_LOCALE_ROOT
  ? path.resolve(process.env.I18N_CHECK_LOCALE_ROOT)
  : path.resolve(ROOT, "frontend/src/shared/i18n/locales");
const SRC_ROOT = process.env.I18N_CHECK_SRC_ROOT
  ? path.resolve(process.env.I18N_CHECK_SRC_ROOT)
  : path.resolve(ROOT, "frontend/src");
const ALLOWLIST_PATH = process.env.I18N_CHECK_ALLOWLIST_PATH
  ? path.resolve(process.env.I18N_CHECK_ALLOWLIST_PATH)
  : path.resolve(ROOT, "scripts/i18n-value-allowlist.json");
const BASE_LOCALE = "en";
const TARGET_LOCALES = ["ko"];

function collectKeys(value, prefix = "") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return [prefix];
  }
  return Object.keys(value).flatMap((key) =>
    collectKeys(value[key], prefix ? `${prefix}.${key}` : key),
  );
}

function valueAt(obj, dotted) {
  let cur = obj;
  for (const part of dotted.split(".")) {
    if (cur == null || typeof cur !== "object") {
      return undefined;
    }
    cur = cur[part];
  }
  return cur;
}

function hasKey(obj, dotted) {
  let cur = obj;
  for (const part of dotted.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) {
      return false;
    }
    cur = cur[part];
  }
  return true;
}

// CLDR plural categories i18next uses to suffix a base key
// (`t("categories.pageCount", { count })` resolves to `pageCount_one` /
// `pageCount_other` at runtime — the bare key never exists in the JSON).
// Missing this made pass 3 flag ~20 real, correctly-pluralized keys as
// orphans on first run against the live tree.
const PLURAL_SUFFIXES = ["_zero", "_one", "_two", "_few", "_many", "_other"];

// `allowPluralFallback` must be false unless the call site actually passes a
// `count` option — otherwise a bare key with no count (a genuine I18N-04
// orphan) can be masked by an unrelated key elsewhere in the same namespace
// that happens to end in one of these six suffixes (e.g. "list.loadMore" is
// wrongly "covered" by an unrelated "list.loadMore_other").
function hasKeyOrPluralVariant(obj, dotted, allowPluralFallback) {
  if (hasKey(obj, dotted)) {
    return true;
  }
  if (!allowPluralFallback) {
    return false;
  }
  return PLURAL_SUFFIXES.some((suffix) => hasKey(obj, `${dotted}${suffix}`));
}

async function readJson(locale, fileName) {
  const filePath = path.join(LOCALE_ROOT, locale, fileName);
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function listSourceFiles(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      // Skip generated/vendored trees and test fixtures — orphan-key
      // detection targets application code, not mocks that intentionally
      // exercise nonexistent keys.
      if (entry.name === "node_modules" || entry.name === "tests") {
        continue;
      }
      files.push(...(await listSourceFiles(full)));
    } else if (/\.(tsx?|jsx?)$/.test(entry.name)) {
      files.push(full);
    }
  }
  return files;
}

// Parses the argument list of a call whose opening "(" is at `openParenIndex`,
// respecting nested (), {}, [], and string/template literals so a comma or
// brace inside a nested object doesn't split the args early. Returns the
// substring of each top-level argument, or null if the parens never close
// (this happens for source that isn't actually a call — false match).
function splitTopLevelArgs(source, openParenIndex) {
  let depth = 0;
  let argStart = openParenIndex + 1;
  const args = [];
  let inString = null;
  // Start just past the call's own "(" — starting on it would count it as a
  // nested-bracket open and throw off every depth comparison below (the
  // matching ")" would then be seen at depth 1, never depth 0).
  for (let i = argStart; i < source.length; i++) {
    const ch = source[i];
    if (inString) {
      if (ch === "\\") {
        i++;
      } else if (ch === inString) {
        inString = null;
      }
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      continue;
    }
    if (ch === "(" || ch === "{" || ch === "[") {
      depth++;
    } else if (ch === ")" || ch === "}" || ch === "]") {
      if (ch === ")" && depth === 0) {
        args.push(source.slice(argStart, i));
        return args.map((a) => a.trim()).filter((a) => a.length > 0);
      }
      depth--;
    } else if (ch === "," && depth === 0) {
      args.push(source.slice(argStart, i));
      argStart = i + 1;
    }
  }
  return null;
}

// Depth-aware split on a top-level delimiter char, respecting nested
// (), {}, [], and string/template literals. `text` has no outer wrapper —
// callers slice that off first. Shared by parseTopLevelObjectProps below.
function splitTopLevelOn(text, delimiter) {
  const parts = [];
  let depth = 0;
  let start = 0;
  let inString = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (ch === "\\") {
        i++;
      } else if (ch === inString) {
        inString = null;
      }
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      continue;
    }
    if (ch === "(" || ch === "{" || ch === "[") {
      depth++;
    } else if (ch === ")" || ch === "}" || ch === "]") {
      depth--;
    } else if (ch === delimiter && depth === 0) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(text.slice(start));
  return parts;
}

// Parses an object-literal argument's *top-level* properties into a
// name -> raw-value-text map. Critical for options like
// `{ ns: "shell", defaultValue: warning.message }` where the value can
// itself be an arbitrary expression (including another `t(...)` call with
// its own `ns:` option) — a plain substring search for `ns\s*:` would match
// that nested call's option instead of the outer one's (this is exactly
// what happened with AccountSecurityScreen.tsx's nested
// `t(\`status.${x}\`, { ns: "shell", ... })` inside an outer auth-namespace
// call before this was scoped to depth).
function parseTopLevelObjectProps(optionsText) {
  const trimmed = optionsText.trim();
  const props = new Map();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) {
    return props;
  }
  const inner = trimmed.slice(1, -1);
  for (const rawEntry of splitTopLevelOn(inner, ",")) {
    const entry = rawEntry.trim();
    if (!entry) continue;
    const [rawName, ...rawValueParts] = splitTopLevelOn(entry, ":");
    if (rawValueParts.length === 0) {
      // ES6 shorthand property ({ count } === { count: count }) — a bare
      // identifier, not "...spread" or a computed key. `t("k", { count,
      // defaultValue: ... })` is the idiomatic way to pass a plural count,
      // so without this a genuine count-bearing call would be
      // indistinguishable from one with no count at all.
      const shorthand = rawName.trim();
      if (/^[A-Za-z_$][\w$]*$/.test(shorthand)) {
        props.set(shorthand, shorthand);
      }
      continue;
    }
    const propName = rawName.trim().replace(/^["'`]|["'`]$/g, "");
    props.set(propName, rawValueParts.join(":").trim());
  }
  return props;
}

function parseStringLiteral(text) {
  const trimmed = text.trim();
  const m = /^(["'`])((?:(?!\1).)*)\1$/.exec(trimmed);
  if (!m) {
    return null;
  }
  // Template literals with an interpolated expression (`ns:${x}`) aren't a
  // static key we can check — the backtick match above still lets those
  // through as a literal since `(?!\1)` only excludes the delimiter, so
  // reject any remaining ${ explicitly.
  if (m[1] === "`" && m[2].includes("${")) {
    return null;
  }
  return m[2];
}

// Finds `useTranslation(...)` bindings in a file: `const { t } = useTranslation("ns")`
// and aliased forms `const { t: tX } = useTranslation("ns")`. Namespace is
// omitted when useTranslation() is called with no arguments (i18next then
// resolves against defaultNS, "shell" in this app).
function findTranslationBindings(source) {
  const bindings = new Map(); // alias -> namespace
  const re = /const\s*\{([^}]*)\}\s*=\s*useTranslation\(\s*(?:\[\s*["'`]([\w-]+)["'`]|["'`]([\w-]+)["'`])?\s*\)/g;
  let m;
  while ((m = re.exec(source))) {
    const destructure = m[1];
    const ns = m[2] ?? m[3] ?? "shell";
    for (const part of destructure.split(",")) {
      const entry = part.trim();
      if (!entry) continue;
      const aliasMatch = /^t\s*:\s*(\w+)$/.exec(entry);
      if (aliasMatch) {
        bindings.set(aliasMatch[1], ns);
      } else if (entry === "t") {
        bindings.set("t", ns);
      }
    }
  }
  return bindings;
}

// Finds every `<alias>(...)` call for a known translation-function alias (or
// the standalone `translate` helper) and resolves each to a {ns, key} pair
// when possible. Calls that can't be statically resolved (no local
// useTranslation binding, no inline `ns:` override, no `ns:key` prefix, and
// not the colon-keyed `translate()` helper) are skipped rather than
// guessed — this is what keeps the 404-screen-style false positives out
// (component bound to one namespace, key looked up in another only because
// we mis-attributed it).
function findResolvedCalls(source, bindings) {
  const aliasNames = new Set([...bindings.keys(), "translate"]);
  const calls = [];
  // The optional "i18n." prefix recognises the shared i18next singleton
  // called directly (`i18n.t(...)`) outside any component's useTranslation
  // binding -- e.g. shared/lib/activityLabels.ts, shared/api/client.ts,
  // SettingsScreen.tsx's non-JSX validators. Without it these calls were
  // structurally invisible to this gate: the bare-identifier regex below
  // never matched "t" when preceded by "." at all. "i18n" is this project's
  // one, unaliased name for the singleton (see shared/i18n/index.ts's
  // `export { i18n }`; every import site spells it exactly this way, never
  // renamed). The lookbehind still anchors the *start* of the match, so
  // "foo.i18n.t(" (i18n as someone else's property) is correctly rejected,
  // not just a bare "foo.t(".
  const callSiteRe = /(?<![.\w$])(i18n\.)?([A-Za-z_$][\w$]*)\s*\(/g;
  let m;
  while ((m = callSiteRe.exec(source))) {
    const isI18nMemberCall = Boolean(m[1]);
    const name = m[2];
    if (isI18nMemberCall) {
      if (name !== "t") continue; // only i18n.t(...) is a translation call
    } else if (!aliasNames.has(name)) {
      continue;
    }
    const openParen = callSiteRe.lastIndex - 1;
    const args = splitTopLevelArgs(source, openParen);
    if (!args || args.length === 0) continue;
    const rawKey = parseStringLiteral(args[0]);
    if (rawKey === null) continue; // dynamic key — can't check statically

    const props = args[1] ? parseTopLevelObjectProps(args[1]) : new Map();

    let ns = null;
    let key = rawKey;
    if (rawKey.includes(":")) {
      const idx = rawKey.indexOf(":");
      ns = rawKey.slice(0, idx);
      key = rawKey.slice(idx + 1);
    } else if (props.has("ns")) {
      const nsValue = parseStringLiteral(props.get("ns"));
      if (nsValue !== null) {
        ns = nsValue;
      }
    }
    if (ns === null) {
      // A direct i18n.t("key", ...) call has no local useTranslation binding
      // to infer a namespace from — same reasoning as "translate" below, it
      // is unresolvable without an explicit ns (colon prefix or `ns:` prop).
      if (isI18nMemberCall || name === "translate") continue;
      ns = bindings.get(name) ?? null;
    }
    if (ns === null) continue; // e.g. a `t` received as a function parameter

    // Only calls carrying a fallback are the documented I18N-04 trap
    // (silently renders English when the key is missing); require it here
    // so we don't also flag legitimate suspense-mode lookups that throw on
    // a real miss. `translate(key, fallback)` takes its fallback as a plain
    // positional 2nd argument (it's `i18n.t(key, { defaultValue: fallback })`
    // under the hood — see shared/i18n/index.ts) rather than a `{ defaultValue }`
    // property, so it doesn't go through parseTopLevelObjectProps at all.
    const hasFallback = name === "translate" ? args.length >= 2 : props.has("defaultValue");
    if (!hasFallback) continue;

    calls.push({ ns, key, hasCount: props.has("count") });
  }
  return calls;
}

async function main() {
  const namespaceFiles = (await readdir(path.join(LOCALE_ROOT, BASE_LOCALE)))
    .filter((fileName) => fileName.endsWith(".json"))
    .sort();

  let failed = false;
  let keyCount = 0;
  const baseByFile = new Map();

  // --- Pass 1: key-set parity between en and each target locale ---
  for (const fileName of namespaceFiles) {
    const base = await readJson(BASE_LOCALE, fileName);
    baseByFile.set(fileName, base);
    const baseKeys = new Set(collectKeys(base));
    keyCount += baseKeys.size;

    for (const locale of TARGET_LOCALES) {
      const targetKeys = new Set(collectKeys(await readJson(locale, fileName)));
      const missing = [...baseKeys].filter((key) => !targetKeys.has(key));
      const extra = [...targetKeys].filter((key) => !baseKeys.has(key));

      if (missing.length > 0 || extra.length > 0) {
        failed = true;
        console.error(`${locale}/${fileName} does not match ${BASE_LOCALE}/${fileName}`);
        if (missing.length > 0) {
          console.error(`  Missing: ${missing.join(", ")}`);
        }
        if (extra.length > 0) {
          console.error(`  Extra: ${extra.join(", ")}`);
        }
      }
    }
  }

  // --- Pass 2: value identity — a ko value byte-identical to its en value
  // passed the key-set check above but is still untranslated English (this
  // is exactly how I18N-03 shipped: keys matched, values didn't move).
  const allowlist = new Set(JSON.parse(await readFile(ALLOWLIST_PATH, "utf8")));
  const usedAllowlistEntries = new Set();
  for (const fileName of namespaceFiles) {
    const base = baseByFile.get(fileName);
    for (const locale of TARGET_LOCALES) {
      const target = await readJson(locale, fileName);
      for (const key of collectKeys(base)) {
        const baseValue = valueAt(base, key);
        const targetValue = valueAt(target, key);
        if (JSON.stringify(baseValue) !== JSON.stringify(targetValue)) {
          continue;
        }
        const allowKey = `${fileName}:${key}`;
        if (allowlist.has(allowKey)) {
          usedAllowlistEntries.add(allowKey);
          continue;
        }
        failed = true;
        console.error(
          `${locale}/${fileName} ${key} is identical to ${BASE_LOCALE}/${fileName} ` +
            `(value: ${JSON.stringify(baseValue)}) — translate it, or add ` +
            `"${allowKey}" to scripts/i18n-value-allowlist.json if this is an ` +
            "intentional proper noun / format-only string.",
        );
      }
    }
  }
  const staleAllowlistEntries = [...allowlist].filter(
    (entry) => !usedAllowlistEntries.has(entry),
  );
  if (staleAllowlistEntries.length > 0) {
    failed = true;
    console.error(
      `scripts/i18n-value-allowlist.json has ${staleAllowlistEntries.length} stale ` +
        "entries that no longer point at an identical en/ko value (remove them so " +
        "the allowlist stays honest):",
    );
    for (const entry of staleAllowlistEntries) {
      console.error(`  ${entry}`);
    }
  }

  // --- Pass 3: orphan keys — a t("key", { defaultValue: "..." }) call whose
  // key does not exist in the base locale silently renders the English
  // defaultValue forever (I18N-04). This walks frontend/src for calls we can
  // statically resolve to a namespace and checks the key exists in en.
  const sourceFiles = await listSourceFiles(SRC_ROOT);
  for (const filePath of sourceFiles) {
    const source = await readFile(filePath, "utf8");
    if (
      !source.includes("useTranslation")
      && !source.includes("translate(")
      && !source.includes("i18n.t(")
    ) {
      continue;
    }
    const bindings = findTranslationBindings(source);
    const calls = findResolvedCalls(source, bindings);
    for (const { ns, key, hasCount } of calls) {
      const base = baseByFile.get(`${ns}.json`);
      if (!base) {
        // Unknown namespace — not this gate's concern (would already fail
        // to load at runtime; namespace typos are out of scope here).
        continue;
      }
      if (!hasKeyOrPluralVariant(base, key, hasCount)) {
        failed = true;
        const relPath = path.relative(ROOT, filePath).replace(/\\/g, "/");
        console.error(
          `${relPath}: t("${key}", { defaultValue: ... }) has no matching ` +
            `key in en/${ns}.json — it will render the English fallback forever`,
        );
      }
    }
  }

  if (failed) {
    process.exitCode = 1;
  } else {
    console.log(
      `i18n coverage OK: ${namespaceFiles.length} namespaces, ${keyCount} ${BASE_LOCALE} keys, ` +
        `${allowlist.size} value-identity allowlist entries`,
    );
  }
}

await main();
