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
  it("draws one neutral list surface and separates rows with a hairline", () => {
    const list = rule(".activity-feed-list");
    expect(list).toMatch(/border:\s*1px solid var\(--border\);/);
    expect(list).toMatch(/background:\s*var\(--surface\);/);
    expect(rule(".activity-feed-list > .activity-feed-entry + .activity-feed-entry")).toMatch(/border-top:\s*1px solid var\(--border\);/);
  });

  it("keeps people and the system neutral and leaves color to sections, AI and status", () => {
    const neutral = rule('.activity-actor-pill[data-actor="human"],\n.activity-actor-pill[data-actor="system"]');
    expect(neutral).toContain("--av-bg: var(--surface-active);");
    expect(neutral).toContain("--av-ink: var(--ink);");
    expect(rule('.activity-actor-pill[data-actor="agent"]')).toContain("--av-bg: var(--library-soft);");
    expect(rule(".activity-actor-split__ai")).toContain("background: var(--agent-soft);");
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
    expect(title).toMatch(/margin-top:\s*4px;/);
    expect(title).toMatch(/gap:\s*4px;/);
  });

  it("provides 24px pointer targets and 32px touch targets without adding row height", () => {
    const actor = rule(".activity-actor-disclosure");
    expect(actor).toMatch(/min-width:\s*24px;/);
    expect(actor).toMatch(/height:\s*24px;/);
    expect(rule(".activity-entry__title-link")).toMatch(/min-height:\s*24px;/);
    expect(css).toMatch(/@media \(pointer: coarse\) \{\s*\.activity-actor-disclosure \{ height: 32px; margin-top: -8px; \}/);
    expect(rule(".activity-feed-see-all")).toMatch(/min-height:\s*44px;/);
  });

  it("references no undefined design token", () => {
    for (const ghost of ["--border-subtle", "--surface-sunken"]) {
      expect(css).not.toContain(`var(${ghost})`);
    }
  });
});
