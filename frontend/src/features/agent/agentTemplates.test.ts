import { describe, expect, it } from "vitest";

import { AGENT_TEMPLATES, BLANK_TEMPLATE_ID, findAgentTemplate } from "./agentTemplates";

describe("agent templates registry", () => {
  it("ships the karpathy-agent-guidelines template, scoped to agent_guide", () => {
    const template = findAgentTemplate("karpathy-agent-guidelines");
    expect(template).toBeDefined();
    // The body is the verbatim upstream markdown imported via ?raw.
    expect(template!.body).toContain("Think Before Coding");
    expect(template!.body.length).toBeGreaterThan(500);
    // It is offered under the Agent guide (AGENT.md) category only.
    expect(template!.categories).toContain("agent_guide");
  });

  it("does not register a 'blank' entry — Blank maps to the default heading", () => {
    expect(findAgentTemplate(BLANK_TEMPLATE_ID)).toBeUndefined();
  });

  it("uses unique ids, non-empty bodies, and namespaced i18n keys", () => {
    const ids = AGENT_TEMPLATES.map((template) => template.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const template of AGENT_TEMPLATES) {
      expect(template.body.trim().length).toBeGreaterThan(0);
      expect(template.nameKey).toMatch(/^templates\.list\./);
      expect(template.descriptionKey).toMatch(/^templates\.list\./);
    }
  });
});
