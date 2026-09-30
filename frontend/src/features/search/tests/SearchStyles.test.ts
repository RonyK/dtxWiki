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

  it("joins the match reason and open action into one bordered box", () => {
    // The wrapper owns the border and radius so the two segments cannot drift
    // to different heights the way the old separate chips did (20px vs 24px).
    expect(css).toMatch(
      /\.search-source-chip\s*\{[^}]*border:\s*1px solid var\(--border\);[^}]*border-radius:\s*7px;[^}]*overflow:\s*hidden;/s,
    );
    for (const seg of [
      "\\.search-source-chip__reason",
      "\\.search-source-chip__open",
    ]) {
      expect(css).toMatch(new RegExp(`${seg}\\s*\\{[^}]*min-height:\\s*30px;`, "s"));
    }
    // The divider only appears when there IS a reason segment to divide from.
    expect(css).toMatch(
      /\.search-source-chip__reason \+ \.search-source-chip__open\s*\{[^}]*border-left:\s*1px solid var\(--border\);/s,
    );
  });

  it("lifts both joined segments to 44px on narrow or touch layouts", () => {
    expect(css).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*\.search-source-chip__reason,\s*\.search-source-chip__open\s*\{[^}]*min-height:\s*44px;/,
    );
  });

  it("gives the chip row more air than the title-to-snippet gap", () => {
    expect(css).toMatch(/\.search-result-body\s*\{[^}]*gap:\s*6px;/s);
    expect(css).toMatch(/\.search-result-chips\s*\{[^}]*margin-top:\s*4px;/s);
  });

  it("retires the pre-redesign search chip classes", () => {
    // .search-open-btn and .search-match-reason were the two chips whose
    // heights disagreed; leaving their rules behind would let a stale caller
    // silently re-introduce the mismatch.
    for (const gone of [".search-open-btn", ".search-match-reason", ".search-result-submeta"]) {
      expect(css).not.toContain(`${gone} {`);
    }
  });
});
