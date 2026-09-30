import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

const css = readAppStyles();

describe("Activity row geometry", () => {
  it("keeps the row card neutral -- identity lives in the pill, not the surface", () => {
    expect(css).toMatch(
      /\.activity-row\s*\{[^}]*border:\s*1px solid var\(--border\);[^}]*border-radius:\s*8px;[^}]*background:\s*var\(--surface\);/s,
    );
  });

  it("tones the actor pill per kind without a fourth, uncovered actor_kind", () => {
    for (const [kind, token] of [
      ["human", "--context"],
      ["agent", "--library"],
      ["system", "--ink-muted"],
    ]) {
      expect(css).toMatch(
        new RegExp(
          `\\.activity-actor-pill\\[data-actor="${kind}"\\]\\s*\\{[^}]*--av-tone:\\s*var\\(${token}\\);`,
          "s",
        ),
      );
    }
  });

  it("splits the MCP actor pill instead of collapsing to one generic label", () => {
    expect(css).toMatch(/\.activity-actor-split__owner\s*\{[^}]*background:/s);
    expect(css).toMatch(/\.activity-actor-split__ai\s*\{[^}]*background:/s);
    // An owner-less client keeps the AI half's tone, not a blank left half.
    expect(css).toMatch(/\.activity-actor-split__ai:only-child\s*\{/);
  });

  it("gives the chip row more air than the message-to-detail gap", () => {
    // .activity-row__body's grid gap (6px) covers the tight message->detail
    // pairing; the chip row is a distinct component and gets extra margin on
    // top of that, not the same 6px for every line.
    expect(css).toMatch(
      /\.activity-row__body\s*\{[^}]*gap:\s*6px;/s,
    );
    expect(css).toMatch(/\.activity-row__chips\s*\{[^}]*margin-top:\s*4px;/s);
  });

  it("lifts the target chip to 44px on narrow or touch layouts", () => {
    expect(css).toMatch(
      /@media \(max-width: 1024px\), \(any-pointer: coarse\)[\s\S]*a\.activity-source-chip\s*\{[^}]*min-height:\s*44px;/,
    );
  });

  it("references no undefined design token", () => {
    for (const ghost of ["--border-subtle", "--surface-sunken"]) {
      expect(css).not.toContain(`var(${ghost})`);
    }
  });
});
