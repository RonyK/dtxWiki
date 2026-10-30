import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import { useOptionalWorkspaceContext } from "../../../shared/workspace/WorkspaceContext";
import { WorkspaceLinkBadge } from "../WorkspaceLinkBadge";

vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../shared/workspace/WorkspaceContext")>();
  return { ...actual, useOptionalWorkspaceContext: vi.fn() };
});

const source = "11111111-1111-4111-8111-111111111111";
const foreign = "22222222-2222-4222-8222-222222222222";
const page = "33333333-3333-4333-8333-333333333333";

describe("WorkspaceLinkBadge", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    vi.mocked(useOptionalWorkspaceContext).mockReturnValue(null);
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows a neutral noninteractive annotation without workspace context", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(
      <a href={`/p/${foreign}/${page}`}>
        Open linked page <WorkspaceLinkBadge href={`/p/${foreign}/${page}`} sourceWorkspaceId={source} />
      </a>,
  );

    const badge = screen.getByText("Other workspace");
    expect(badge.tagName).toBe("SPAN");
    expect(screen.getByText("Opens another workspace; access is checked after navigation.")).toHaveClass("visually-hidden");
    expect(badge).not.toHaveAttribute("tabindex");
    fireEvent.pointerEnter(badge);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Open linked page Opens another workspace; access is checked after navigation." })).toBeInTheDocument();
    fetchSpy.mockRestore();
  });

  it("uses the title from an exact workspace membership match", () => {
    vi.mocked(useOptionalWorkspaceContext).mockReturnValue({
      workspaces: [{ id: foreign, title: "Research Team", name: "Research Team" }],
    } as ReturnType<typeof useOptionalWorkspaceContext>);

    render(
      <a href={`/workspaces/${foreign}/context`}>
        Open linked workspace <WorkspaceLinkBadge href={`/workspaces/${foreign}/context`} sourceWorkspaceId={source} />
      </a>,
    );

    const badge = screen.getByText("Research Team");
    expect(screen.getByText("Opens workspace Research Team; access is checked after navigation.")).toHaveClass("visually-hidden");
    expect(badge.closest(".workspace-link-badge")).toHaveClass("workspace-link-badge--known");
  });
});
