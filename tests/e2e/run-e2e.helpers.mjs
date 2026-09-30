// Fix-6 Task 3.3 (M-F5-CODEX-3): Windows argument escaping for run-e2e.mjs.
// Extracted into a helper module so unit tests can exercise the escape
// algorithm without spawning subprocesses.

import path from "node:path";

// cmd.exe + MSVCRT layered escaping.
//
// Rules:
//   - MSVCRT (Microsoft Visual C Runtime, the default argv parser for
//     Node.exe / Playwright / npx.cmd children): backslash is literal
//     UNLESS it precedes a quote, in which case it escapes the quote.
//     A run of N backslashes followed by a quote -> emit 2N+1 backslashes
//     plus the escaped quote. A trailing run of N backslashes before the
//     closing wrapper quote -> emit 2N backslashes.
//   - cmd.exe (the outer shell): inside `"..."` quoting, `& | < > ^ ( )`
//     are TREATED LITERALLY, so the surrounding quotes protect them. Only
//     `%var%` (environment expansion) and `!var!` (delayed expansion) can
//     still expand inside double quotes — caret-escape `%` and `!` to
//     suppress them.
//
// We deliberately do NOT caret-escape `& | < > ^ ( )` — that would
// over-escape and break literal values like `OAuth & PAT` (a Playwright
// grep pattern) by leaving `^&` in the argument string.
const CMD_META = /[%!"\s&|<>()^]/;
const CARET_META = /([%!])/g;

export function escapeWindowsArg(value) {
  if (value.length > 0 && !CMD_META.test(value)) {
    return value;
  }
  let escaped = "";
  for (let i = 0; i < value.length; i += 1) {
    const ch = value[i];
    if (ch === "\\") {
      let runLength = 0;
      while (i < value.length && value[i] === "\\") {
        runLength += 1;
        i += 1;
      }
      const nextIsQuote = i < value.length && value[i] === '"';
      if (nextIsQuote) {
        escaped += "\\".repeat(runLength * 2 + 1) + '\\"';
        i += 1;
      } else if (i === value.length) {
        escaped += "\\".repeat(runLength * 2);
      } else {
        escaped += "\\".repeat(runLength);
      }
      i -= 1;
    } else if (ch === '"') {
      escaped += '\\"';
    } else {
      escaped += ch;
    }
  }
  return `"${escaped.replace(CARET_META, "^$1")}"`;
}

export function buildWindowsCommandLine(command, args) {
  return [command, ...args].map(escapeWindowsArg).join(" ");
}

export function resolveSafeE2ETempDir(candidate, systemTempDir) {
  const resolvedCandidate = path.resolve(candidate);
  const resolvedTempDir = path.resolve(systemTempDir);
  const relative = path.relative(resolvedTempDir, resolvedCandidate);
  const expectedPrefix = "dtxwiki-e2e-";
  const leaf = path.basename(relative);

  if (
    !relative ||
    path.dirname(relative) !== "." ||
    !leaf.startsWith(expectedPrefix) ||
    leaf.length === expectedPrefix.length
  ) {
    throw new Error(
      `Refusing to remove unsafe E2E directory: ${resolvedCandidate}. ` +
        `Expected one direct ${expectedPrefix}* child of ${resolvedTempDir}.`,
    );
  }
  return resolvedCandidate;
}
