import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

const css = readFileSync(
  resolve(process.cwd(), "src/styles/09-lint-findings-fixes.css"),
  "utf8",
);

describe("Lint findings geometry", () => {
  it("keeps one full-width card and a 20px checkbox inside a 44px hit area", () => {
    expect(css).toMatch(
      /\.lint-finding-card\s*\{[^}]*width:\s*100%;[^}]*min-width:\s*0;/s,
    );
    expect(css).toMatch(
      /\.lint-finding-card \.lint-row-selector\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;/s,
    );
    expect(css).toMatch(
      /\.lint-finding-card \.lint-row-checkbox,[^{]*\{[^}]*width:\s*20px;[^}]*height:\s*20px;/s,
    );
  });

  it("carries severity in the pill and leaves the card surface neutral", () => {
    for (const [severity, token] of [
      ["error", "--danger"],
      ["warning", "--warning"],
      ["info", "--info"],
    ]) {
      expect(css).toMatch(
        new RegExp(
          `\\.lint-finding-card\\[data-severity="${severity}"\\]\\s*\\{[^}]*--finding-tone:\\s*var\\(${token}\\);`,
          "s",
        ),
      );
    }
    expect(css).toMatch(
      /\.lint-severity-badge\s*\{[^}]*background:\s*color-mix\(in srgb, var\(--finding-tone\)[^}]*\}/s,
    );
    // The card must not paint itself by severity: no accent bar, no wash.
    expect(css).not.toMatch(/border-inline-start:\s*4px solid var\(--finding-tone\)/);
    expect(css).not.toMatch(
      /\.lint-finding-card\[data-severity\]\s*\{[^}]*background:\s*color-mix/s,
    );
  });

  it("joins the rule and target segments into one bordered box", () => {
    // The wrapper owns the border and radius so the two segments cannot drift
    // to different heights or corner radii again.
    expect(css).toMatch(
      /\.lint-source-chip\s*\{[^}]*border:\s*1px solid var\(--border\);[^}]*border-radius:\s*7px;[^}]*overflow:\s*hidden;/s,
    );
    expect(css).toMatch(
      /\.lint-target-chip\s*\{[^}]*border-left:\s*1px solid var\(--border\);[^}]*text-decoration:\s*none;/s,
    );
    for (const chip of ["\\.lint-rule-chip", "\\.lint-target-chip"]) {
      expect(css).toMatch(new RegExp(`${chip}\\s*\\{[^}]*min-height:\\s*30px;`, "s"));
    }
  });

  it("gives the chip row more air than the message-to-detail gap", () => {
    // .lint-finding-body's grid gap (6px) covers the tight message->detail
    // pairing; the chip row is a distinct component and gets extra margin on
    // top of that (matches .activity-row__chips in the Activity feed).
    expect(css).toMatch(/\.lint-finding-body\s*\{[^}]*gap:\s*6px;/s);
    expect(css).toMatch(
      /\.lint-finding-chips\s*\{[^}]*margin:\s*4px 0 0;/s,
    );
  });

  it("lifts both joined segments to 44px on narrow or touch layouts", () => {
    // Raising only the anchor left the rule segment 20px short and tore the
    // joined box apart at tablet width.
    expect(css).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*\.lint-rule-chip,\s*\.lint-target-chip\s*\{[^}]*min-height:\s*44px;/,
    );
  });

  it("references no undefined design token in the bundled stylesheet", () => {
    // `--border-subtle` and `--surface-sunken` are never defined, so every
    // shorthand naming them was invalid at computed-value time and silently
    // rendered nothing -- which is how the lint details panel lost its rule
    // and the bulk toolbar lost its background.
    const bundle = readAppStyles();
    for (const ghost of ["--border-subtle", "--surface-sunken"]) {
      expect(bundle).not.toContain(`var(${ghost})`);
    }
  });

  it("preserves 44px pager and menu targets for narrow or touch layouts", () => {
    expect(css).toMatch(
      /\.action-menu__trigger\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;/s,
    );
    expect(css).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*\.numeric-pagination__page,[^{]*\{[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;/,
    );
  });
});
