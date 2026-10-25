import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

const css = readAppStyles();

describe("Search result geometry", () => {
  it("keeps the result card neutral -- kind lives in the pill, not the surface", () => {
    expect(css).toMatch(
      /\.search-result-row\s*\{[^}]*border:\s*1px solid var\(--border\);[^}]*border-radius:\s*8px;[^}]*background:\s*var\(--surface\);/s,
    );
  });

  it("tones the kind pill per result identity", () => {
    for (const [identity, token] of [
      ["context", "--context"],
      ["library", "--library"],
      ["agent", "--agent"],
      ["review", "--review"],
      ["muted", "--ink-muted"],
    ]) {
      expect(css).toMatch(
        new RegExp(
          `\\.search-result-kind\\[data-identity="${identity}"\\]\\s*\\{[^}]*--kind-tone:\\s*var\\(${token}\\);`,
          "s",
        ),
      );
    }
  });

  it("distinguishes the neutral location from the informational reason", () => {
    expect(css).toMatch(
      /\.search-result-location\s*\{[^}]*color:\s*var\(--ink-muted\);/s,
    );
    expect(css).toMatch(
      /\.search-result-reason\s*\{[^}]*color:\s*var\(--info\);/s,
    );
  });

  it("stacks the kind above a three-line title on mobile", () => {
    expect(css).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.search-result-title\s*\{[^}]*flex-direction:\s*column;/,
    );
    expect(css).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.search-result-title-text\s*\{[^}]*-webkit-line-clamp:\s*3;/,
    );
  });

  it("spaces the reason separately and provides a visible keyboard focus ring", () => {
    expect(css).toMatch(/\.search-result-body\s*\{[^}]*gap:\s*6px;/s);
    expect(css).toMatch(/\.search-result-reason\s*\{[^}]*margin-top:\s*4px;/s);
    expect(css).toMatch(/a\.search-result-row:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--info\);/s);
  });

  it("retires the pre-redesign search chip classes", () => {
    for (const gone of [
      ".search-open-btn", ".search-match-reason", ".search-result-submeta",
      ".search-source-chip", ".search-source-chip__reason", ".search-source-chip__open",
    ]) {
      expect(css).not.toContain(`${gone} {`);
    }
  });
});
