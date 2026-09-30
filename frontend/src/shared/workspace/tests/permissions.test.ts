import { describe, expect, it } from "vitest";

import type { WorkspaceSummary } from "../../api/workspace";
import { workspaceDeleteDisabledReason } from "../permissions";

const baseWorkspace: WorkspaceSummary = {
  id: "workspace-1",
  slug: "research",
  title: "Research",
  name: "Research",
  goal: "Research work",
  default_language: "en",
  visibility: "private",
  role: "owner",
};

describe("workspaceDeleteDisabledReason", () => {
  it("allows owners to delete their only workspace", () => {
    expect(workspaceDeleteDisabledReason(baseWorkspace)).toBeNull();
  });

  it("allows owners to delete a workspace whose slug is default", () => {
    expect(
      workspaceDeleteDisabledReason({ ...baseWorkspace, slug: "default" }),
    ).toBeNull();
  });

  it("blocks deletion for non-owners", () => {
    expect(
      workspaceDeleteDisabledReason({ ...baseWorkspace, role: "member" }),
    ).toBe("not_owner");
  });
});
