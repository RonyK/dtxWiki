import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

const css = readAppStyles();

function rule(selector: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`^\\s*${escapedSelector}\\s*\\{([^}]+)\\}`, "m"));
  if (!match) throw new Error(`Missing activity style rule: ${selector}`);
  return match[1];
}

describe("Shared activity feed geometry", () => {
  it("keeps the shared entry surface neutral -- identity lives in the pill, not the surface", () => {
    const entry = rule(".activity-feed-entry");
    expect(entry).toMatch(/border:\s*1px solid var\(--border\);/);
    expect(entry).toMatch(/border-radius:\s*8px;/);
    expect(entry).toMatch(/background:\s*var\(--surface\);/);
  });

  it("tones the actor pill per kind without a fourth, uncovered actor_kind", () => {
    for (const [kind, token] of [
      ["human", "--context"],
      ["agent", "--library"],
      ["system", "--ink-muted"],
    ]) {
      expect(rule(`.activity-actor-pill[data-actor="${kind}"]`)).toContain(`--av-tone: var(${token});`);
    }
  });

  it("splits the MCP actor pill instead of collapsing to one generic label", () => {
    expect(rule(".activity-actor-split__owner")).toContain("background:");
    expect(rule(".activity-actor-split__ai")).toContain("background:");
    // An owner-less client keeps the AI half's tone, not a blank left half.
    expect(rule(".activity-actor-split__ai:only-child")).toMatch(/border-left:\s*0;/);
  });

  it("keeps type and actor adjacent with compact chip-to-title and description spacing", () => {
    expect(rule(".activity-entry__identity")).toMatch(/gap:\s*8px;/);
    const title = rule(".activity-entry__title-link, .activity-entry__plain-title");
    expect(title).toMatch(/padding-top:\s*4px;/);
    expect(title).toMatch(/gap:\s*4px;/);
  });

  it("provides 44px actor and navigation targets without relying on the removed source chip", () => {
    const actor = rule(".activity-actor-disclosure");
    expect(actor).toMatch(/min-width:\s*44px;/);
    expect(actor).toMatch(/height:\s*44px;/);
    for (const selector of [
      ".activity-entry__title-link",
      ".activity-entry__review-link",
      ".activity-feed-see-all",
    ]) {
      expect(rule(selector)).toMatch(/min-height:\s*44px;/);
    }
  });

  it("references no undefined design token", () => {
    for (const ghost of ["--border-subtle", "--surface-sunken"]) {
      expect(css).not.toContain(`var(${ghost})`);
    }
  });
});
