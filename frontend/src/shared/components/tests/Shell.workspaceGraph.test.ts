import { describe, expect, it } from "vitest";

import { navGroupsForLabels } from "../Shell";

describe("Shell Workspace Knowledge Map flag", () => {
  it("adds a Preview /graph item only when the release flag is enabled", () => {
    const labels = {
      groups: { knowledge: "Knowledge", workflow: "Workflow", workspace: "Workspace" },
      nav: {
        dashboard: "Dashboard",
        library: "Library",
        context: "Context",
        graph: "Knowledge Map",
        agent: "Agent",
        chats: "Chats",
        ask: "Ask",
        review: "Review",
        lint: "Lint",
        activity: "Activity",
        search: "Search",
        settings: "Settings",
      },
    } as unknown as Parameters<typeof navGroupsForLabels>[0];
    expect(navGroupsForLabels(labels, false).flatMap((group) => group.items)
      .some((item) => item.to === "/graph")).toBe(false);
    expect(navGroupsForLabels(labels, true).flatMap((group) => group.items))
      .toEqual(expect.arrayContaining([
        expect.objectContaining({
          key: "graph",
          label: "Knowledge Map",
          preview: true,
          to: "/graph",
        }),
      ]));
  });
});
