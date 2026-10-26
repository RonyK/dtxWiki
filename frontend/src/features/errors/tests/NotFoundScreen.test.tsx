import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigationType } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import { createReferenceReturnState } from "../../../shared/lib/navigationRecovery";
import { NotFoundScreen } from "../NotFoundScreen";

const { workspaceContext } = vi.hoisted(() => ({ workspaceContext: { current: null as unknown } }));
vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../shared/workspace/WorkspaceContext")>();
  return { ...actual, useOptionalWorkspaceContext: () => workspaceContext.current };
});

const WORKSPACE_A = "22222222-2222-4222-8222-000000000001";
const WORKSPACE_B = "22222222-2222-4222-8222-000000000002";
const PAGE_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_PAGE_ID = "33333333-3333-4333-8333-333333333334";

function renderNotFound(
  pathname: string,
  workspaces: Array<{ id: string; title: string }> = [],
  state?: unknown,
) {
  workspaceContext.current = { workspaces };
  return render(
    <MemoryRouter initialEntries={[{ pathname, state }]}>
      <NotFoundScreen />
      <LocationStateProbe />
    </MemoryRouter>,
  );
}

function LocationStateProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return <output data-testid="location-state">{JSON.stringify({ pathname: location.pathname, search: location.search, hash: location.hash, state: location.state, navigationType })}</output>;
}

describe("NotFoundScreen", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    workspaceContext.current = null;
  });

  it("keeps malformed routes on the ordinary 404 surface", () => {
    renderNotFound("/wrong/path");

    expect(screen.getByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Find workspaces" })).toHaveAttribute("href", "/workspaces/find");
    expect(screen.getByRole("link", { name: "Account" })).toHaveAttribute("href", "/account");
    expect(screen.queryByRole("heading", { name: "This item can't be opened" })).toBeNull();
  });

  it("treats an unrecognized Workspace suffix as malformed rather than an unavailable item", () => {
    renderNotFound(`/workspaces/${WORKSPACE_B}/context/not-a-route`);

    expect(screen.getByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "This item can't be opened" })).toBeNull();
  });

  it("does not reuse the failed Workspace URL when there are no memberships", () => {
    renderNotFound(`/workspaces/${WORKSPACE_B}/context/pages/${PAGE_ID}`);

    expect(screen.getByRole("heading", { name: "This item can't be opened" })).toBeInTheDocument();
    expect(screen.getByText(/may be missing.*current account may not have access/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Find workspaces" })).toHaveAttribute("href", "/workspaces/find");
    expect(screen.queryByRole("link", { name: new RegExp(WORKSPACE_B) })).toBeNull();
    expect(screen.queryByRole("link", { name: "Return to previous page" })).toBeNull();
  });

  it("offers a compact workspace chooser and rejects the same entity through a stable permalink", async () => {
    renderNotFound(
      `/workspaces/${WORKSPACE_B}/context/pages/${PAGE_ID}`,
      [
        { id: WORKSPACE_A, title: "Research" },
        { id: WORKSPACE_B, title: "Operations" },
      ],
      createReferenceReturnState(`/p/${WORKSPACE_B}/${PAGE_ID}`),
    );

    await userEvent.click(screen.getByRole("button", { name: "Select a workspace" }));
    expect(screen.getByRole("combobox", { name: "Your workspaces" })).toHaveDisplayValue("Research");
    expect(screen.getByRole("button", { name: "Open Research dashboard" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Return to previous page" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Find workspaces" })).toBeNull();
  });

  it("accepts a trusted prior page only when its Workspace is in the current membership list, then clears it on return", async () => {
    renderNotFound(
      `/workspaces/${WORKSPACE_B}/context/pages/${PAGE_ID}`,
      [
        { id: WORKSPACE_A, title: "Research" },
        { id: WORKSPACE_B, title: "Operations" },
      ],
      createReferenceReturnState(`/workspaces/${WORKSPACE_A}/context/pages/${OTHER_PAGE_ID}?tab=history#v3`),
    );

    expect(screen.getByRole("link", { name: "Return to previous page" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_A}/context/pages/${OTHER_PAGE_ID}?tab=history#v3`,
    );
    await userEvent.click(screen.getByRole("button", { name: "Select a workspace" }));
    const recoveryDialog = screen.getByRole("dialog", { name: "Select a workspace" });
    const workspaceSelect = within(recoveryDialog).getByRole("combobox", { name: "Your workspaces" });
    expect(workspaceSelect).toHaveDisplayValue("Research");
    await userEvent.selectOptions(workspaceSelect, WORKSPACE_B);
    expect(within(recoveryDialog).getByRole("button", { name: "Open Operations dashboard" })).toBeInTheDocument();
    await userEvent.click(within(recoveryDialog).getByRole("button", { name: "Close workspace chooser" }));
    await userEvent.click(screen.getByRole("link", { name: "Return to previous page" }));
    expect(JSON.parse(screen.getByTestId("location-state").textContent ?? "{}")).toEqual({
      pathname: `/workspaces/${WORKSPACE_A}/context/pages/${OTHER_PAGE_ID}`,
      search: "?tab=history",
      hash: "#v3",
      state: null,
      navigationType: "REPLACE",
    });
    await userEvent.click(screen.getByRole("button", { name: "Select a workspace" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Your workspaces" }), WORKSPACE_B);
    await userEvent.click(screen.getByRole("button", { name: "Open Operations dashboard" }));
    expect(JSON.parse(screen.getByTestId("location-state").textContent ?? "{}")).toEqual({
      pathname: `/workspaces/${WORKSPACE_B}/dashboard`,
      search: "",
      hash: "",
      state: null,
      navigationType: "REPLACE",
    });
  });

  it("rejects external previous-route state", () => {
    renderNotFound(
      `/workspaces/${WORKSPACE_B}/context/pages/${PAGE_ID}`,
      [{ id: WORKSPACE_A, title: "Research" }],
      createReferenceReturnState("https://evil.example/account"),
    );
    expect(screen.queryByRole("link", { name: "Return to previous page" })).toBeNull();
  });

  it("rejects a malformed internal previous route", () => {
    renderNotFound(
      `/workspaces/${WORKSPACE_B}/context/pages/${PAGE_ID}`,
      [{ id: WORKSPACE_A, title: "Research" }],
      createReferenceReturnState(`/workspaces/${WORKSPACE_A}/context/not-a-route`),
    );
    expect(screen.queryByRole("link", { name: "Return to previous page" })).toBeNull();
  });
});
