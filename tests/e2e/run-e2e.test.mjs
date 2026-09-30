// Fix-6 Task 3.3 (M-F5-CODEX-3) regression: run-e2e.mjs Windows arg escaping.
//
// Run with `node tests/e2e/run-e2e.test.mjs`. Exits non-zero on first failure.
// This file is intentionally a single-file self-runner (no test framework)
// because run-e2e.mjs is a build-tool helper, not application code, and
// pulling in a runner would create a new infrastructure surface that
// e2e itself depends on.

import { strict as assert } from "node:assert";
import { tmpdir } from "node:os";

const {
  escapeWindowsArg,
  buildWindowsCommandLine,
  resolveSafeE2ETempDir,
} = await import("./run-e2e.helpers.mjs");

const cases = [
  {
    label: "alphanumeric stays unquoted",
    input: "playwright",
    expected: "playwright",
  },
  { label: "empty stays quoted", input: "", expected: '""' },
  {
    label: "space triggers quoting (& protected by quotes, no caret needed)",
    input: "two words",
    expected: '"two words"',
  },
  {
    label: "double quote MSVCRT escape",
    input: 'a"b',
    expected: '"a\\"b"',
  },
  {
    label: "ampersand stays literal inside quotes",
    input: "a&b",
    expected: '"a&b"',
  },
  {
    label: "pipe stays literal inside quotes",
    input: "a|b",
    expected: '"a|b"',
  },
  {
    label: "percent caret-escaped (still expands inside quotes)",
    input: "a%PATH%b",
    expected: '"a^%PATH^%b"',
  },
  {
    label: "bang caret-escaped (delayed expansion can expand inside quotes)",
    input: "a!FOO!b",
    expected: '"a^!FOO^!b"',
  },
  {
    label: "windows path with no metachar stays as-is",
    input: "C:\\path",
    expected: "C:\\path",
  },
  {
    label: "grep with metacharacter pattern",
    input: '--grep="OAuth & PAT"',
    expected: '"--grep=\\"OAuth & PAT\\""',
  },
];

let failed = 0;
for (const c of cases) {
  try {
    assert.equal(escapeWindowsArg(c.input), c.expected);
    process.stdout.write(`  ok  ${c.label}\n`);
  } catch (err) {
    failed += 1;
    process.stdout.write(`  FAIL ${c.label}: ${err.message}\n`);
  }
}

const fullLine = buildWindowsCommandLine("npx.cmd", [
  "playwright",
  "test",
  "--grep",
  "OAuth & revoke",
]);
try {
  assert.equal(
    fullLine,
    'npx.cmd playwright test --grep "OAuth & revoke"',
  );
  process.stdout.write("  ok  full command line\n");
} catch (err) {
  failed += 1;
  process.stdout.write(`  FAIL full command line: ${err.message}\n`);
}

// The vite build step run-e2e.mjs runs before Playwright. On Windows this argv
// is what reaches cmd.exe, so the npm `--` separator must survive verbatim and
// the mkdtemp outDir must survive a space in the user profile path.
const buildLine = buildWindowsCommandLine("npm.cmd", [
  "--workspace",
  "frontend",
  "exec",
  "--",
  "vite",
  "build",
  "--outDir",
  "C:\\Users\\A B\\AppData\\Local\\Temp\\dtxwiki-e2e-x\\spa",
  "--emptyOutDir",
]);
try {
  assert.equal(
    buildLine,
    "npm.cmd --workspace frontend exec -- vite build --outDir " +
      '"C:\\Users\\A B\\AppData\\Local\\Temp\\dtxwiki-e2e-x\\spa" --emptyOutDir',
  );
  process.stdout.write("  ok  spa build command line\n");
} catch (err) {
  failed += 1;
  process.stdout.write(`  FAIL spa build command line: ${err.message}\n`);
}

const separator = process.platform === "win32" ? "\\" : "/";
const tempRoot = tmpdir();
const safeTempDir = `${tempRoot}${separator}dtxwiki-e2e-Ab12cd`;
try {
  assert.equal(resolveSafeE2ETempDir(safeTempDir, tempRoot), safeTempDir);
  assert.throws(
    () => resolveSafeE2ETempDir(tempRoot, tempRoot),
    /Refusing to remove unsafe E2E directory/,
  );
  assert.throws(
    () => resolveSafeE2ETempDir(`${tempRoot}${separator}other-run`, tempRoot),
    /Refusing to remove unsafe E2E directory/,
  );
  assert.throws(
    () => resolveSafeE2ETempDir(
      `${tempRoot}${separator}nested${separator}dtxwiki-e2e-Ab12cd`,
      tempRoot,
    ),
    /Refusing to remove unsafe E2E directory/,
  );
  process.stdout.write("  ok  E2E temp cleanup target validation\n");
} catch (err) {
  failed += 1;
  process.stdout.write(`  FAIL E2E temp cleanup target validation: ${err.message}\n`);
}

if (failed > 0) {
  process.stdout.write(`\n${failed} test(s) failed\n`);
  process.exit(1);
}
process.stdout.write(`\nAll ${cases.length + 3} tests passed\n`);
