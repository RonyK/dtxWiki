import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { readAppStyles } from "./appStyles";

const STYLES_DIR = join(process.cwd(), "src/styles");

function cssFiles(): string[] {
  return readdirSync(STYLES_DIR).filter((file) => file.endsWith(".css"));
}

function importedPartials(): string[] {
  const index = readFileSync(join(STYLES_DIR, "index.css"), "utf-8");
  return [...index.matchAll(/@import\s+"\.\/([^"]+)"/g)].map((match) => match[1]);
}

describe("app stylesheet integrity", () => {
  it("uses LF-only line endings in every stylesheet (no CRLF)", () => {
    // The split refactor's safety net (readAppStyles + the static CSS tests)
    // matches rules with tolerant indexOf/contains, so a Windows author saving a
    // partial as CRLF slips through silently — which is exactly how the bundle's
    // byte-stability claim was weakened once already. `.gitattributes` pins
    // `*.css text eol=lf`; this is the test backstop that fails loudly if a
    // partial ever lands with carriage returns.
    for (const file of cssFiles()) {
      const text = readFileSync(join(STYLES_DIR, file), "utf-8");
      expect(text.includes("\r"), `${file} contains CR — stylesheets must be LF-only`).toBe(
        false,
      );
    }
  });

  it("wires every partial into index.css exactly once, with no dangling import", () => {
    const imported = importedPartials();
    const partials = cssFiles().filter((file) => file !== "index.css");

    for (const ref of imported) {
      expect(partials, `index.css @imports "${ref}" but no such partial exists`).toContain(
        ref,
      );
    }
    for (const file of partials) {
      expect(imported, `${file} exists but is not @imported by index.css`).toContain(file);
    }
    // A partial imported twice would make its cascade position ambiguous.
    expect(new Set(imported).size, "a partial is @imported more than once").toBe(
      imported.length,
    );
  });

  it("keeps braces balanced across the reconstructed stylesheet", () => {
    // A partial cut mid-rule (a boundary landing inside `{ ... }`) would leave
    // the concatenation unbalanced; the styles carry no literal braces in
    // strings, so a raw count is a sound structural check.
    const css = readAppStyles();
    const open = (css.match(/{/g) ?? []).length;
    const close = (css.match(/}/g) ?? []).length;
    expect(open, "unbalanced braces in concatenated stylesheet").toBe(close);
  });

  it("includes textarea in the global form-control font:inherit reset", () => {
    // Form controls don't inherit font-family from their ancestors — the UA
    // stylesheet pins textarea to monospace — so the global reset must list
    // textarea explicitly. Tailwind Preflight covered it; when Preflight was
    // dropped the replacement rule listed only button/input/select, so every
    // unstyled <textarea> (e.g. the editor title) silently fell back to
    // monospace. Guard the bare-element reset that fixes that.
    const css = readAppStyles();
    const hasBareElement = (selector: string, element: string) =>
      selector.split(",").some((part) => part.trim() === element);
    const formFontReset = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(
      ([, selector, body]) =>
        /font:\s*inherit/.test(body) &&
        hasBareElement(selector, "button") &&
        hasBareElement(selector, "input") &&
        hasBareElement(selector, "select"),
    );
    expect(formFontReset, "global form-control font:inherit reset not found").toBeTruthy();
    expect(
      hasBareElement(formFontReset![1], "textarea"),
      "textarea missing from the form-control font:inherit reset — it renders in the UA monospace default",
    ).toBe(true);
  });
});
