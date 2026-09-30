import karpathyAgentGuidelines from "./templates/karpathy-agent-guidelines.md?raw";

// Built-in starter templates for an Agent page body, scoped by category. Adding
// one is: a `.md` under ./templates imported `?raw`, an entry here (with the
// categories it should appear under), and two i18n keys under
// `agent:templates.list.<id>`. The "Blank" choice is not in this list — it maps
// to the existing minimal `defaultAgentBody()` heading.
export const BLANK_TEMPLATE_ID = "blank";

export type AgentTemplate = {
  id: string;
  nameKey: string;
  descriptionKey: string;
  categories: string[];
  body: string;
};

export const AGENT_TEMPLATES: AgentTemplate[] = [
  {
    id: "karpathy-agent-guidelines",
    nameKey: "templates.list.karpathyAgentGuidelines.name",
    descriptionKey: "templates.list.karpathyAgentGuidelines.description",
    categories: ["agent_guide"],
    body: karpathyAgentGuidelines,
  },
];

export function findAgentTemplate(id: string): AgentTemplate | undefined {
  return AGENT_TEMPLATES.find((template) => template.id === id);
}
