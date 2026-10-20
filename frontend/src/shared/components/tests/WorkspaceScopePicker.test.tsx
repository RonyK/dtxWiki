import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useState } from "react";
import {
  WorkspaceScopePicker,
  WorkspaceScopeSelector,
  type WorkspaceScopeOption,
} from "../WorkspaceScopePicker";
import {
  listCredentialWorkspaces,
  type CredentialWorkspacePage,
} from "../../api/credentialWorkspaces";
import { setLocale } from "../../i18n";

vi.mock("../../api/credentialWorkspaces", () => ({
  listCredentialWorkspaces: vi.fn(),
}));
const labels = {
  clearAll: "Clear all",
  defaultBadge: "Default",
  emptySelection: "Select at least one",
  listLabel: "Workspaces",
  membershipMissing: "Remove only",
  noMatches: "No matches",
  searchAria: "Search",
  searchClear: "Clear",
  searchPlaceholder: "Search",
  selectAllShown: "Select page",
  selectedCount: "selected",
  toggleAllAria: "Toggle page",
};
const options: WorkspaceScopeOption[] = Array.from(
  { length: 17 },
  (_, index) => ({
    id: `${index}`,
    title: `Workspace ${String(index).padStart(2, "0")}`,
    slug: `workspace-${index}`,
    isDefault: index === 0,
  }),
);
const response = (indices: number[]): CredentialWorkspacePage => ({
  data: indices.map((i) => ({
    ...options[i],
    role: "owner",
    is_default: i === 0,
  })),
  total_count: indices.length,
  next_cursor: null,
});
beforeEach(() => {
  setLocale("en", { persist: false });
  vi.resetAllMocks();
});

it("keeps stable rows and full selections through pages, search, selected view, and clear", async () => {
  const user = userEvent.setup();
  function Harness() {
    const [selected, setSelected] = useState(["0"]);
    return (
      <>
        <output data-testid="selected">{selected.join(",")}</output>
        <WorkspaceScopePicker
          selectedIds={selected}
          onChange={setSelected}
          labels={labels}
          workspaces={options}
        />
      </>
    );
  }
  render(<Harness />);
  expect(screen.getAllByRole("option")).toHaveLength(8);
  const firstOrder = screen.getAllByRole("option").map((n) => n.textContent);
  await user.click(screen.getByRole("option", { name: /Workspace 03/ }));
  expect(screen.getAllByRole("option").map((n) => n.textContent)).toEqual(
    firstOrder,
  );
  await user.click(screen.getByRole("button", { name: "Next" }));
  await user.click(screen.getByRole("option", { name: /Workspace 08/ }));
  await user.type(screen.getByRole("searchbox"), "workspace-16");
  await user.click(screen.getByRole("option", { name: /Workspace 16/ }));
  await user.clear(screen.getByRole("searchbox"));
  expect(screen.getByTestId("selected")).toHaveTextContent("0,3,8,16");
  await user.click(screen.getByRole("button", { name: /Selected items/ }));
  expect(screen.getAllByRole("option")).toHaveLength(4);
  expect(screen.getAllByRole("option", { selected: true })).toHaveLength(4);
  await user.click(screen.getByRole("button", { name: "Clear all" }));
  expect(screen.getByTestId("selected").textContent).toBe("");
});

it("discards a modal draft on Escape and commits only on Apply", async () => {
  const user = userEvent.setup();
  const changed = vi.fn();
  render(
    <WorkspaceScopeSelector
      selectedIds={["0"]}
      onChange={changed}
      labels={labels}
      workspaces={options}
    />,
  );
  const open = screen.getByRole("button", {
    name: "View and select workspaces",
  });
  await user.click(open);
  await user.click(screen.getByRole("option", { name: /Workspace 01/ }));
  await user.keyboard("{Escape}");
  expect(changed).not.toHaveBeenCalled();
  expect(open).toHaveFocus();
  await user.click(open);
  expect(screen.getByRole("option", { name: /Workspace 01/ })).toHaveAttribute(
    "aria-selected",
    "false",
  );
  await user.click(screen.getByRole("option", { name: /Workspace 01/ }));
  await user.click(screen.getByRole("button", { name: "Apply 2 selected" }));
  expect(changed).toHaveBeenCalledExactlyOnceWith(["0", "1"]);
  expect(open).toHaveFocus();
});

it("ignores a late previous-query response and offers retry on a current failure", async () => {
  const user = userEvent.setup();
  let resolveOld!: (page: CredentialWorkspacePage) => void;
  vi.mocked(listCredentialWorkspaces).mockImplementation((query) =>
    query === ""
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : Promise.resolve(response([16])),
  );
  render(
    <WorkspaceScopePicker
      serverPaging
      selectedIds={["0"]}
      onChange={vi.fn()}
      labels={labels}
      workspaces={options.slice(0, 1)}
    />,
  );
  await user.type(screen.getByRole("searchbox"), "new");
  await screen.findByRole("option", { name: /Workspace 16/ });
  await act(async () => resolveOld(response([1])));
  expect(
    screen.queryByRole("option", { name: /Workspace 01/ }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: /Workspace 16/ }),
  ).toBeInTheDocument();
  vi.mocked(listCredentialWorkspaces).mockRejectedValueOnce(
    new Error("offline"),
  );
  await user.type(screen.getByRole("searchbox"), "x");
  const error = await screen.findByRole("alert");
  expect(
    within(error).getByRole("button", { name: "Retry" }),
  ).toBeInTheDocument();
  await user.click(within(error).getByRole("button", { name: "Retry" }));
  await waitFor(() =>
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
  );
  await screen.findByRole("option", { name: /Workspace 16/ });
});

it("keeps the selected-items pager usable when an all-items request is still pending", async () => {
  const user = userEvent.setup();
  let resolvePage!: (page: CredentialWorkspacePage) => void;
  vi.mocked(listCredentialWorkspaces).mockReturnValue(
    new Promise((resolve) => {
      resolvePage = resolve;
    }),
  );
  render(
    <WorkspaceScopePicker
      serverPaging
      selectedIds={options.map((option) => option.id)}
      onChange={vi.fn()}
      labels={labels}
      workspaces={options}
    />,
  );
  await user.click(screen.getByRole("button", { name: /Selected items/ }));
  expect(screen.queryByText("Loading workspaces…")).not.toBeInTheDocument();
  const next = screen.getByRole("button", { name: "Next" });
  expect(next).toBeEnabled();
  await user.click(next);
  expect(screen.getAllByRole("option")).toHaveLength(8);
  expect(screen.getAllByRole("option")[0]).toHaveTextContent("Workspace 08");
  await act(async () => resolvePage(response([0])));
  expect(screen.getAllByRole("option")[0]).toHaveTextContent("Workspace 08");
});

it("debounces server search and keeps the selection count against all workspaces", async () => {
  const user = userEvent.setup();
  vi.mocked(listCredentialWorkspaces).mockImplementation(async (query) => ({
    ...response(query ? [16] : [0, 1, 2]),
    total_count: query ? 1 : 17,
  }));
  render(
    <WorkspaceScopePicker
      serverPaging
      selectedIds={["0", "1", "2"]}
      onChange={vi.fn()}
      labels={labels}
      workspaces={options.slice(0, 3)}
    />,
  );
  await screen.findByRole("option", { name: /Workspace 02/ });
  expect(screen.getByText("3/17 selected")).toBeInTheDocument();
  await user.type(screen.getByRole("searchbox"), "workspace-16");
  await screen.findByRole("option", { name: /Workspace 16/ });
  expect(
    vi.mocked(listCredentialWorkspaces).mock.calls.map(([query]) => query),
  ).toEqual(["", "workspace-16"]);
  expect(screen.getByText("3/17 selected")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /Selected items/ }));
  expect(screen.getByText("3/17 selected")).toBeInTheDocument();
});

it("shows no matches only after the current server request succeeds with an empty page", async () => {
  let resolvePage!: (page: CredentialWorkspacePage) => void;
  vi.mocked(listCredentialWorkspaces).mockReturnValue(
    new Promise((resolve) => { resolvePage = resolve; }),
  );
  render(
    <WorkspaceScopePicker serverPaging selectedIds={[]} onChange={vi.fn()} labels={labels} workspaces={[]} />,
  );
  expect(screen.getByText("Loading workspaces…")).toBeInTheDocument();
  expect(screen.queryByText("No matches")).not.toBeInTheDocument();
  await act(async () => resolvePage(response([])));
  expect(screen.queryByText("Loading workspaces…")).not.toBeInTheDocument();
  expect(screen.getByText("No matches")).toBeInTheDocument();
});

it("keeps failure and retry states distinct from a successful empty result", async () => {
  const user = userEvent.setup();
  let resolveRetry!: (page: CredentialWorkspacePage) => void;
  vi.mocked(listCredentialWorkspaces)
    .mockRejectedValueOnce(new Error("offline"))
    .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve; }));
  render(
    <WorkspaceScopePicker serverPaging selectedIds={[]} onChange={vi.fn()} labels={labels} workspaces={[]} />,
  );
  const alert = await screen.findByRole("alert");
  expect(screen.queryByText("No matches")).not.toBeInTheDocument();
  await user.click(within(alert).getByRole("button", { name: "Retry" }));
  expect(screen.getByText("Loading workspaces…")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("No matches")).not.toBeInTheDocument();
  await act(async () => resolveRetry(response([])));
  expect(screen.getByText("No matches")).toBeInTheDocument();
});

it("hides a stale empty result while a new search is debouncing or loading", async () => {
  vi.useFakeTimers();
  try {
    let resolveSearch!: (page: CredentialWorkspacePage) => void;
    vi.mocked(listCredentialWorkspaces)
      .mockResolvedValueOnce(response([]))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSearch = resolve; }));
    await act(async () => {
      render(
        <WorkspaceScopePicker serverPaging selectedIds={[]} onChange={vi.fn()} labels={labels} workspaces={[]} />,
      );
    });
    expect(screen.getByText("No matches")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "new" } });
    expect(listCredentialWorkspaces).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("No matches")).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(listCredentialWorkspaces).toHaveBeenLastCalledWith("new", null);
    expect(screen.getByText("Loading workspaces…")).toBeInTheDocument();
    expect(screen.queryByText("No matches")).not.toBeInTheDocument();
    await act(async () => resolveSearch(response([16])));
    expect(screen.getByRole("option", { name: /Workspace 16/ })).toBeInTheDocument();
    expect(screen.queryByText("No matches")).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

it("can show an empty selected view while the all-items request is still pending", async () => {
  const user = userEvent.setup();
  vi.mocked(listCredentialWorkspaces).mockReturnValue(new Promise(() => {}));
  render(
    <WorkspaceScopePicker serverPaging selectedIds={[]} onChange={vi.fn()} labels={labels} workspaces={[]} />,
  );
  await user.click(screen.getByRole("button", { name: /Selected items/ }));
  expect(screen.queryByText("Loading workspaces…")).not.toBeInTheDocument();
  expect(screen.getByText("No matches")).toBeInTheDocument();
});
