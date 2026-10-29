import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LintScreen } from "../LintScreen";

// LintScreen gates mutation affordances on the workspace role (LG-F3). Default
// to owner so the action-bearing tests render the buttons; the viewer test flips
// the role.
const { ctxWorkspace } = vi.hoisted(() => ({
  ctxWorkspace: {
    current: { selectedWorkspace: { role: "owner" } } as Record<
      string,
      unknown
    >,
  },
}));
vi.mock(
  "../../../shared/workspace/WorkspaceContext",
  async (importOriginal) => ({
    ...(await importOriginal<object>()),
    useOptionalWorkspaceContext: () => ctxWorkspace.current,
  }),
);

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";

describe("LintScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    ctxWorkspace.current = { selectedWorkspace: { role: "owner" } };
  });

  it("renders findings, creates runs, and decides findings", async () => {
    let decidedBody: unknown = null;
    let decisionCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: decisionCount === 0 ? [finding()] : [],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs`) {
        return jsonResponse({ data: lintRun() }, 202);
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings/finding-1/decision`
      ) {
        decidedBody = JSON.parse(String(init?.body));
        decisionCount += 1;
        return jsonResponse({ data: { ...finding(), status: "acknowledged" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    const findingCard = screen
      .getByText("missing citation")
      .closest(".lint-finding-card") as HTMLElement;
    expect(findingCard).toBeInTheDocument();
    expect(findingCard.querySelector(".operations-row")).toBeNull();
    // No disclosure: the message and the backend's plain `suggested_action`
    // are both readable without a click. The old renderer JSON.parsed
    // "Add citation", threw, and printed "No structured suggestion is
    // available." in its place.
    expect(
      within(findingCard).getByText(
        "Review the quality issue for Context page.",
      ),
    ).toBeInTheDocument();
    expect(within(findingCard).getByText("Add citation")).toBeInTheDocument();
    expect(
      within(findingCard).queryByRole("button", { name: /details/i }),
    ).toBeNull();
    expect(findingCard).toHaveAttribute("data-severity", "warning");
    await userEvent.click(screen.getByRole("button", { name: "Run lint" }));
    expect(await screen.findByText("Lint run queued")).toBeInTheDocument();

    expect(screen.queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Mark reviewed" })).toBeNull();
    const moreActions = screen.getByRole("button", {
      name: /More actions for missing citation/i,
    });
    await userEvent.click(moreActions);
    expect(
      screen.getByRole("menuitem", { name: "Mark reviewed" }),
    ).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(
      screen.getByRole("menuitem", { name: "Record as fixed" }),
    ).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(moreActions).toHaveFocus();
    await userEvent.click(moreActions);
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Mark reviewed" }),
    );
    expect(decidedBody).toEqual({
      decision: "acknowledge",
      reason: null,
    });
    expect(
      await screen.findByText("No unresolved lint findings"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Finding marked as reviewed."),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Findings" }),
      ).toHaveFocus(),
    );
  });

  it("filters findings by the selected severity chip", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 640px)",
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&severity=error&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              {
                ...finding(),
                id: "finding-2",
                rule_id: "broken_link",
                severity: "error",
              },
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
        <LintHistoryProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    const filterToggle = screen.getByRole("button", { name: "Filters" });
    expect(filterToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("radio", { name: "Error" })).toBeNull();
    await userEvent.click(filterToggle);
    expect(filterToggle).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(
      screen.getByRole("checkbox", { name: /Select missing citation/i }),
    );
    expect(screen.getAllByText("1 selected").length).toBeGreaterThan(0);
    // Selecting the Error chip refetches with the severity query param.
    await userEvent.click(screen.getByRole("radio", { name: "Error" }));
    expect(await screen.findByText("broken link")).toBeInTheDocument();
    expect(screen.getByTestId("lint-location")).toHaveTextContent(
      "?severity=error",
    );
    expect(screen.queryByText("missing citation")).not.toBeInTheDocument();
    expect(screen.queryAllByText("1 selected")).toHaveLength(0);
    expect(
      screen.getByRole("checkbox", { name: /Select broken link/i }),
    ).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Test back" }));
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    expect(screen.getByTestId("lint-location")).toHaveTextContent("(empty)");
  });

  it("uses exact numeric pagination and replaces the visible page", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [finding()],
            total: 21,
            page: 1,
            page_size: 20,
            page_count: 2,
            has_previous: false,
            has_next: true,
            has_more: true,
            next_cursor: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=2&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              { ...finding(), id: "finding-2", rule_id: "broken_link" },
            ],
            total: 21,
            page: 2,
            page_size: 20,
            page_count: 2,
            has_previous: true,
            has_next: false,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    expect(screen.getByText("21")).toBeInTheDocument();
    expect(screen.getByText("Showing 1–20 of 21 findings")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Go to page 2" }));
    expect(await screen.findByText("broken link")).toBeInTheDocument();
    expect(screen.queryByText("missing citation")).not.toBeInTheDocument();
    expect(screen.getByText("Showing 21–21 of 21 findings")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Page 2, current page" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("renders a single Lint crumb without a fake Activity parent", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({ data: { findings: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("No unresolved lint findings"),
    ).toBeInTheDocument();
    // The screen is one level deep, so there is no trail to draw — the heading
    // alone names the page, and no invented parent appears above it.
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(screen.queryByText("Activity")).not.toBeInTheDocument();
  });

  it("surfaces a banner when triggering a lint run fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs`) {
        return jsonResponse(errorBody("lint run failed"), 500);
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run lint" }));

    const errors = await screen.findAllByText(
      "The server could not complete the request. Please try again.",
    );
    expect(errors[0]).toBeInTheDocument();
    expect(screen.queryByText(/Lint run/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run lint" })).toBeEnabled();
  });

  it("surfaces a banner and keeps the finding when a decision fails", async () => {
    let rejectDecision!: (reason: unknown) => void;
    const decisionResponse = new Promise<Response>((_resolve, reject) => {
      rejectDecision = reject;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings/finding-1/decision`
      ) {
        return decisionResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    const moreActions = screen.getByRole("button", {
      name: /More actions for missing citation/i,
    });
    await userEvent.click(moreActions);
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Ignore finding" }),
    );
    await userEvent.type(
      screen.getByLabelText("Reason (optional)"),
      "Still unresolved",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Ignore finding" }),
    );

    await waitFor(() => expect(moreActions).toBeDisabled());
    expect(screen.getByRole("button", { name: "Run lint" })).toBeDisabled();

    rejectDecision(new Error("raw req-secret"));

    const errors = await screen.findAllByText(
      "Failed to update the lint finding",
    );
    expect(errors[0]).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
    expect(screen.getByText("missing citation")).toBeInTheDocument();
    expect(moreActions).toBeEnabled();
  });

  it("hides run and decision actions from viewers but still shows findings (LG-F3)", async () => {
    ctxWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    // Findings stay readable for viewers...
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    // ...but the editor-only mutation affordances are gone (they would 403).
    expect(screen.queryByRole("button", { name: "Run lint" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /More actions for missing citation/i }),
    ).toBeNull();
    expect(screen.queryByRole("menuitem")).toBeNull();
    expect(screen.getByText(/read-only access/i)).toBeInTheDocument();
  });

  it("shows no read-only banner before the workspace role resolves (Sweep-3)", async () => {
    // No selectedWorkspace yet (context still loading) -> role unknown: hide the
    // mutating actions (fail safe) but do NOT flash the read-only banner.
    ctxWorkspace.current = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    expect(screen.queryByText(/read-only access/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Run lint" })).toBeNull();
  });

  it("filters by maintenance layer and links a coverage gap to its finding", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&layer=maintenance&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              { ...finding(), rule_id: "maintenance.nav_summary_suggestion" },
            ],
            next_cursor: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/coverage-gaps?status=open`
      ) {
        return jsonResponse({
          data: {
            items: [
              {
                id: "gap-1",
                workspace_id: WORKSPACE_ID,
                query_norm: "missing concept",
                surface: "search_rest",
                tried_lanes: ["entity"],
                nearest_candidates: [],
                status: "open",
                hit_count: 2,
                created_at: "2026-07-12T00:00:00Z",
                resolved_at: null,
                finding_id: "finding-1",
              },
            ],
            has_more: true,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    await openFilters();
    await userEvent.click(screen.getByRole("radio", { name: "Maintenance" }));
    expect(
      await screen.findByText("Navigation summary proposal"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Coverage gaps" }));
    const link = await screen.findByRole("link", {
      name: "Open linked finding",
    });
    expect(link).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/lint/findings/finding-1");
    expect(screen.getByText("1+")).toBeInTheDocument();
  });

  it("keeps acknowledged findings actionable and hides terminal decisions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [finding({ status: "acknowledged" })],
            next_cursor: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=resolved&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding({ status: "fixed" })], next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: /More actions for missing citation/i }),
    );
    expect(screen.queryByRole("menuitem", { name: "Mark reviewed" })).toBeNull();
    expect(
      screen.getByRole("menuitem", { name: "Record as fixed" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("menuitem", { name: "Ignore finding" }),
    ).toBeEnabled();

    await openFilters();
    await userEvent.click(screen.getByRole("radio", { name: "Resolved" }));
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /More actions for missing citation/i }),
    ).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Record as fixed" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Ignore finding" })).toBeNull();
  });

  it("cancels dismiss without issuing a decision request", async () => {
    let decisionRequested = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (url.includes("/decision")) decisionRequested = true;
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("button", {
        name: /More actions for missing citation/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Ignore finding" }),
    );
    expect(
      screen.getByRole("dialog", { name: "Ignore finding" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(decisionRequested).toBe(false);
    expect(screen.getByText("missing citation")).toBeInTheDocument();
  });

  it("lands on a linked finding and hides internal provenance identifiers", async () => {
    const targetId = "11111111-1111-4111-8111-111111111111";
    const taskId = "22222222-2222-4222-8222-222222222222";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?finding_id=finding-1&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              finding({
                target_id: targetId,
                suggested_action: JSON.stringify({
                  source: "client_llm",
                  task_id: taskId,
                  actor_id: "internal-actor",
                  client_llm_verdict: {
                    verdict: "distinct",
                    negative_cues: ["Policy belongs elsewhere."],
                    task_id: taskId,
                  },
                }),
              }),
            ],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint/findings/finding-1"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Showing the finding linked from this coverage gap.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("AI suggestion")).toBeInTheDocument();
    expect(screen.getByText("Policy belongs elsewhere.")).toBeInTheDocument();
    expect(screen.queryByText(taskId)).toBeNull();
    expect(screen.queryByText("internal-actor")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Open target Context page" }),
    ).toHaveAttribute("href", `/workspaces/00000000-0000-4000-8000-000000000001/context/pages/${targetId}`);
    // The view switch is a tab bar now; the rest stay filter groups.
    expect(screen.getByRole("tab", { name: "Findings" })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Coverage gaps" }),
    ).toBeInTheDocument();
    await openFilters();
    for (const label of ["Status", "Layer", "Severity"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("hides a previously loaded finding while a different typed target loads", async () => {
    let resolveSecond!: (response: Response) => void;
    let secondRequestStarted = false;
    const secondFinding = new Promise<Response>((resolve) => { resolveSecond = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`) {
        return jsonResponse({ data: { findings: [
          finding({ id: "finding-1", suggested_action: "First route action" }),
          finding({ id: "finding-2", suggested_action: "Second route action" }),
        ], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?finding_id=finding-2&page=1&page_size=20`) {
        secondRequestStarted = true;
        return secondFinding;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
        <LintRouteSwitch />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Lint queue" });
    expect(screen.getAllByRole("button", { name: /More actions for missing citation/i }).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Navigate to finding-2" }));
    await waitFor(() => expect(secondRequestStarted).toBe(true));
    expect(screen.queryByRole("button", { name: /More actions/ })).toBeNull();
    expect(screen.queryByText("Second route action")).toBeNull();
    await act(async () => {
      resolveSecond(jsonResponse({ data: { findings: [finding({ id: "finding-2", suggested_action: "Second route action" })], next_cursor: null } }));
    });
    expect(await screen.findByText("Second route action")).toBeInTheDocument();
  });

  it("reloads the latest filters when a queued run completes", async () => {
    const timerCallbacks = captureLintPollTimers();
    let resolvePoll!: (response: Response) => void;
    let openRequests = 0;
    let errorRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        openRequests += 1;
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&severity=error&page=1&page_size=20`
      ) {
        errorRequests += 1;
        return jsonResponse({
          data: {
            findings: [
              {
                ...finding(),
                id: "finding-2",
                rule_id: "broken_link",
                severity: "error",
              },
            ],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs`) {
        return jsonResponse({ data: lintRun() }, 202);
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs/run-1`) {
        return new Promise<Response>((resolve) => {
          resolvePoll = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run lint" }));
    expect(await screen.findByText("Lint run queued")).toBeInTheDocument();
    await waitFor(() => expect(timerCallbacks).toHaveLength(1));
    act(() => timerCallbacks.shift()?.());
    await waitFor(() => expect(resolvePoll).toBeDefined());

    await openFilters();
    await userEvent.click(screen.getByRole("radio", { name: "Error" }));
    expect(await screen.findByText("broken link")).toBeInTheDocument();
    const openRequestsBeforeCompletion = openRequests;

    await act(async () => {
      resolvePoll(jsonResponse({ data: lintRun({ status: "completed" }) }));
    });

    // The banner is cleared once the run finishes: the refreshed list is the
    // result, so a "still running" strip would outlive its subject.
    await waitFor(() => expect(screen.queryByText(/Lint run/)).toBeNull());
    await waitFor(() => expect(errorRequests).toBeGreaterThanOrEqual(2));
    expect(openRequests).toBe(openRequestsBeforeCompletion);
  });

  it("reloads once the poll budget is exhausted by a run that never settles", async () => {
    const timerCallbacks = captureLintPollTimers();
    let findingRequests = 0;
    let pollAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        findingRequests += 1;
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs`) {
        return jsonResponse({ data: lintRun() }, 202);
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs/run-1`) {
        pollAttempts += 1;
        return jsonResponse({ data: lintRun({ status: "running" }) });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run lint" }));
    const requestsAfterRun = findingRequests;

    // Drain the whole 30-tick budget without the run ever settling.
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await waitFor(() => expect(timerCallbacks).toHaveLength(1));
      await act(async () => {
        timerCallbacks.shift()?.();
      });
    }

    await waitFor(() => expect(pollAttempts).toBe(30));
    // The list must still refresh once, instead of being stranded on the
    // pre-run snapshot forever.
    await waitFor(() =>
      expect(findingRequests).toBeGreaterThan(requestsAfterRun),
    );
  });

  it("continues polling after a transient run-status failure", async () => {
    const timerCallbacks = captureLintPollTimers();
    let pollAttempts = 0;
    let findingRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        findingRequests += 1;
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs`) {
        return jsonResponse({ data: lintRun() }, 202);
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/runs/run-1`) {
        pollAttempts += 1;
        if (pollAttempts === 1) throw new Error("temporary network failure");
        return jsonResponse({ data: lintRun({ status: "completed" }) });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByText("missing citation")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run lint" }));
    await waitFor(() => expect(timerCallbacks).toHaveLength(1));
    act(() => timerCallbacks.shift()?.());
    await waitFor(() => expect(pollAttempts).toBe(1));
    await waitFor(() => expect(timerCallbacks).toHaveLength(1));
    act(() => timerCallbacks.shift()?.());

    // Completion clears the progress banner and refreshes the list instead.
    await waitFor(() => expect(screen.queryByText(/Lint run/)).toBeNull());
    await waitFor(() => expect(findingRequests).toBeGreaterThanOrEqual(3));
    expect(pollAttempts).toBe(2);
  });

  it("links page and tree targets while keeping null targets as text", async () => {
    const contextId = "11111111-1111-4111-8111-111111111111";
    const treeId = "22222222-2222-4222-8222-222222222222";
    const secondTreeId = "33333333-3333-4333-8333-333333333333";
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              finding({
                id: "context-finding",
                rule_id: "context_target",
                target_id: contextId,
              }),
              finding({
                id: "tree-finding",
                rule_id: "tree_target",
                target_type: "workspace_tree_node",
                target_id: treeId,
              }),
              finding({
                id: "second-tree-finding",
                rule_id: "tree_target",
                target_type: "workspace_tree_node",
                target_id: secondTreeId,
              }),
              finding({
                id: "null-finding",
                rule_id: "null_target",
                target_id: null,
              }),
            ],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?include_archived=true`) {
        return jsonResponse({
          data: [treeId, secondTreeId].map((id) => ({
            id,
            section: "context",
            title: id === treeId ? "Workspace folder" : "Second workspace folder",
            node_kind: "folder",
            parent_id: null,
            sort_key: "0001",
            target_kind: null,
            target_id: null,
            is_system_node: false,
            can_have_children: true,
            primary_placement: true,
            visibility_status: id === secondTreeId ? "archived" : "visible",
          })),
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    const contextRow = (await screen.findByText("context target")).closest(
      ".lint-finding-card",
    ) as HTMLElement;
    const treeRows = screen
      .getAllByText("tree target")
      .map((text) => text.closest(".lint-finding-card") as HTMLElement);
    const nullRow = screen
      .getByText("null target")
      .closest(".lint-finding-card") as HTMLElement;
    expect(
      within(contextRow).getByRole("link", {
        name: "Open target Context page",
      }),
    ).toHaveAttribute("href", `/workspaces/00000000-0000-4000-8000-000000000001/context/pages/${contextId}`);
    expect(
      within(treeRows[0]).getByRole("link", {
        name: "Open target Workspace folder",
      }),
    ).toHaveAttribute("href", `/workspaces/00000000-0000-4000-8000-000000000001/context/folders/${treeId}`);
    expect(
      within(treeRows[1]).getByRole("link", {
        name: "Open target Workspace folder",
      }),
    ).toHaveAttribute("href", `/workspaces/00000000-0000-4000-8000-000000000001/context/folders/${secondTreeId}`);
    expect(fetchSpy.mock.calls.filter(([input]) => String(input).endsWith("/tree?include_archived=true"))).toHaveLength(1);
    expect(fetchSpy.mock.calls.some(([input]) => String(input).includes("/tree/nodes/"))).toBe(false);
    expect(within(nullRow).queryByRole("link")).toBeNull();
    expect(within(nullRow).getByText("Context page")).toBeInTheDocument();
  });

  it("supports bulk selection, indeterminate select-all, and partial failure", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [
              finding({ id: "finding-1", rule_id: "broken_link" }),
              finding({
                id: "finding-2",
                rule_id: "missing_citation",
                severity: "error",
              }),
            ],
            next_cursor: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings/batch/decision`
      ) {
        return jsonResponse({
          data: {
            updated: [finding({ id: "finding-1" })],
            failed: [
              {
                id: "finding-2",
                error_code: "internal_error",
                message: "Already fixed",
              },
            ],
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("broken link")).toBeInTheDocument();

    // Check indeterminate select-all when partially selected
    const selectAllCheckbox = screen.getByRole("checkbox", {
      name: /Select all 2/i,
    });
    expect(selectAllCheckbox).not.toBeChecked();

    const selectFirst = screen.getByRole("checkbox", {
      name: /Select broken link/i,
    });
    expect(selectFirst).not.toHaveClass("visually-hidden");
    expect(selectFirst).toHaveClass("lint-row-checkbox");
    await userEvent.click(selectFirst);
    expect(selectAllCheckbox).toHaveProperty("indeterminate", true);
    await userEvent.click(selectAllCheckbox);

    // Verify bulk toolbar actions
    const bulkToolbar = screen.getByRole("toolbar", { name: "Bulk actions" });
    const ackButton = within(bulkToolbar).getByRole("button", {
      name: "Mark reviewed",
    });
    expect(ackButton).toBeInTheDocument();
    expect(
      within(bulkToolbar).queryByRole("button", { name: "Record as fixed" }),
    ).toBeNull(); // Should not exist in bulk toolbar

    // Acknowledge both findings; the server accepts one and rejects one.
    await userEvent.click(ackButton);

    expect(
      await screen.findByText("1 findings updated; 1 failed."),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Failed to update 1 findings."),
    ).toBeInTheDocument();
    const failedRow = screen
      .getByText("missing citation")
      .closest(".lint-finding-card") as HTMLElement;
    // Recovery focus lands on the card itself: it is the only element every
    // finding has now that the details disclosure is gone (a viewer sees
    // neither a checkbox nor an action menu).
    await waitFor(() => expect(failedRow).toHaveFocus());
    expect(
      within(failedRow).getByRole("checkbox", {
        name: /Select missing citation/i,
      }),
    ).toBeChecked();
  });

  it("canonicalizes page size to 20 and keeps selection page-scoped", async () => {
    const firstPage = Array.from({ length: 20 }, (_, index) =>
      finding({ id: `finding-${String(index + 1).padStart(3, "0")}` }),
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: firstPage,
            total: 21,
            page: 1,
            page_size: 20,
            page_count: 2,
            has_previous: false,
            has_next: true,
            has_more: true,
            next_cursor: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=2&page_size=20`
      ) {
        return jsonResponse({
          data: {
            findings: [finding({ id: "finding-021", rule_id: "broken_link" })],
            total: 21,
            page: 2,
            page_size: 20,
            page_count: 2,
            has_previous: true,
            has_next: false,
            has_more: false,
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint?page_size=100"]}>
        <LintScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("checkbox", {
        name: "Select all 20 on this page",
      }),
    );
    expect(screen.getAllByText("20 selected").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Go to page 2" }));

    expect(await screen.findByText("broken link")).toBeInTheDocument();
    expect(screen.queryAllByText("20 selected")).toHaveLength(0);
    expect(
      screen.getByRole("checkbox", { name: /Select broken link/i }),
    ).not.toBeChecked();
    expect(screen.getByText("Showing 21–21 of 21 findings")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Select all 1 on this page" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Findings per page" }),
    ).not.toBeInTheDocument();
  });

  it("clears selection and disables old-workspace mutations during a switch", async () => {
    const nextWorkspaceId = "00000000-0000-4000-8000-000000000002";
    ctxWorkspace.current = {
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner" },
    };
    let resolveNextWorkspace!: (response: Response) => void;
    const nextWorkspaceResponse = new Promise<Response>((resolve) => {
      resolveNextWorkspace = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return jsonResponse({
          data: { findings: [finding()], next_cursor: null },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${nextWorkspaceId}/lint/findings?status=open&page=1&page_size=20`
      ) {
        return nextWorkspaceResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/lint"]}>
        <LintScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("checkbox", { name: /Select missing citation/i }),
    );
    expect(screen.getAllByText("1 selected").length).toBeGreaterThan(0);

    ctxWorkspace.current = {
      selectedWorkspace: { id: nextWorkspaceId, role: "owner" },
    };
    view.rerender(
      <MemoryRouter key={nextWorkspaceId} initialEntries={[`/workspaces/${nextWorkspaceId}/lint`]}>
        <LintScreen />
      </MemoryRouter>,
    );

    expect(screen.queryAllByText("1 selected")).toHaveLength(0);
    expect(await screen.findByText("Loading Lint")).toBeInTheDocument();

    await act(async () => {
      resolveNextWorkspace(
        jsonResponse({
          data: {
            findings: [
              finding({
                id: "finding-next",
                workspace_id: nextWorkspaceId,
                rule_id: "broken_link",
              }),
            ],
            next_cursor: null,
          },
        }),
      );
      await nextWorkspaceResponse;
    });
    expect(await screen.findByText("broken link")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /Select broken link/i }),
    ).not.toBeChecked();
  });
});

/**
 * Status/Layer/Severity now live behind a disclosure that is collapsed at
 * every width, so their radios are `hidden` until the panel is opened.
 */
async function openFilters() {
  const toggle = screen.getByRole("button", { name: "Filters" });
  if (toggle.getAttribute("aria-expanded") !== "true") {
    await userEvent.click(toggle);
  }
}

function errorBody(message: string) {
  return {
    error_code: "internal_error",
    message,
    request_id: "req-1",
    warnings: [],
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function finding(overrides: Record<string, unknown> = {}) {
  return {
    id: "finding-1",
    lint_run_id: "run-1",
    workspace_id: WORKSPACE_ID,
    target_type: "context_page",
    target_id: null,
    rule_id: "missing_citation",
    severity: "warning",
    status: "open",
    suggested_action: "Add citation",
    created_at: "2026-05-09T00:00:00Z",
    resolved_at: null,
    ...overrides,
  };
}

function lintRun(overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    workspace_id: WORKSPACE_ID,
    lint_scope: { kind: "workspace", targets: [] },
    trigger_type: "manual",
    status: "queued",
    finding_count_by_severity: {},
    created_at: "2026-05-09T00:00:00Z",
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

function captureLintPollTimers() {
  const callbacks: Array<() => void> = [];
  const realSetTimeout = window.setTimeout.bind(window);
  const implementation = (handler: TimerHandler, timeout?: number): number => {
    if (
      timeout === 1_000 &&
      typeof handler === "function" &&
      handler.length === 1
    ) {
      callbacks.push(() => handler());
      const token = realSetTimeout(() => undefined, 0);
      clearTimeout(token);
      return token;
    }
    return realSetTimeout(handler, timeout);
  };
  vi.spyOn(window, "setTimeout").mockImplementation(
    implementation as unknown as typeof setTimeout,
  );
  return callbacks;
}

function LintHistoryProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="lint-location">
        {location.search || "(empty)"}
      </output>
      <button onClick={() => navigate(-1)} type="button">
        Test back
      </button>
    </>
  );
}

function LintRouteSwitch() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/workspaces/00000000-0000-4000-8000-000000000001/lint/findings/finding-2")} type="button">Navigate to finding-2</button>;
}
