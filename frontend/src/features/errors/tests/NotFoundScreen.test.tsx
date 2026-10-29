import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import { NotFoundScreen } from "../NotFoundScreen";

const { workspaceState } = vi.hoisted(() => ({
  workspaceState: { current: null as { workspaces: Array<{ id: string }> } | null },
}));
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => workspaceState.current,
}));

describe("NotFoundScreen", () => {
  const workspaceId = "22222222-2222-4222-8222-000000000001";
  beforeEach(async () => {
    // Other suites may flip the shared i18n singleton to "ko"; assert the
    // English copy under a known locale.
    await i18n.changeLanguage("en");
    workspaceState.current = { workspaces: [{ id: "22222222-2222-4222-8222-000000000001" }] };
  });

  it("renders the 404 heading and a Back to dashboard link", () => {
    render(
      <MemoryRouter initialEntries={[`/workspaces/${workspaceId}/context/pages/33333333-3333-4333-8333-333333333333`]}>
        <NotFoundScreen />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { name: "We couldn't find that page" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to dashboard" }),
    ).toHaveAttribute("href", `/workspaces/${workspaceId}/dashboard`);
    expect(screen.getByRole("link", { name: "Open Context" })).toHaveAttribute(
      "href",
      `/workspaces/${workspaceId}/context`,
    );
  });

  it("uses workspace discovery when no workspace route can be trusted", () => {
    workspaceState.current = { workspaces: [] };
    render(<MemoryRouter initialEntries={["/unknown"]}><NotFoundScreen /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Back to dashboard" })).toHaveAttribute("href", "/workspaces/find");
    expect(screen.getByRole("link", { name: "Open Context" })).toHaveAttribute("href", "/workspaces/find");
  });

  it("does not send a nonmember back into the unavailable workspace", () => {
    workspaceState.current = { workspaces: [{ id: "33333333-3333-4333-8333-333333333333" }] };
    render(<MemoryRouter initialEntries={[`/workspaces/${workspaceId}/context/pages/33333333-3333-4333-8333-333333333333`]}><NotFoundScreen /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Back to dashboard" })).toHaveAttribute("href", "/workspaces/find");
    expect(screen.getByRole("link", { name: "Open Context" })).toHaveAttribute("href", "/workspaces/find");
  });
});
