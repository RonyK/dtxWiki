import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { SetupScreen } from "../SetupScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const labs = vi.hoisted(() => ({ chats: false, agent: false }));

vi.mock("../../../shared/hooks/useLabsPreferences", () => ({
  useLabsPreferences: () => ({ preferences: labs }),
}));

describe("SetupScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    labs.chats = false;
    labs.agent = false;
    setLocale("en", { persist: false });
  });

  it("renders readiness cards from live APIs", async () => {
    labs.agent = true;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [{ id: "context-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [{ id: "agent-1" }],
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Setup" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: /Library Reference materials 1 item Ready to cite Open Library/i,
      }),
    ).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/library");
    expect(screen.getByText("Workspace basics")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Citation & write policy/i }))
      .toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/settings?section=citation-write");
    expect(screen.getByRole("link", { name: /Agent injection/i }))
      .toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/settings?section=agent-injection");
    expect(
      screen.getByRole("link", { name: /Connect external AI tools \(MCP\)/i }),
    ).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/settings?section=mcp");
    expect(screen.getByText("Your workspace is ready.")).toBeInTheDocument();
    expect(screen.getByText("3 of 3 · Ready")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Context" })).toHaveAttribute(
      "href",
      "/workspaces/00000000-0000-4000-8000-000000000001/context",
    );
    expect(screen.queryByRole("link", { name: "Open Ask" })).not.toBeInTheDocument();
  });

  it("uses server totals and labels a compatibility page as a lower bound", async () => {
    labs.agent = true;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
        total: 73,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: Array.from({ length: 50 }, (_, index) => ({
          id: `context-${index + 1}`,
        })),
        total: null,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [],
        total: 0,
        has_more: false,
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("73 items")).toBeInTheDocument();
    expect(screen.getByText("50+ items loaded")).toBeInTheDocument();
    expect(screen.getByText("0 items")).toBeInTheDocument();
  });

  it("shows a retryable error instead of a spinner when Setup fails to load", async () => {
    const user = userEvent.setup();
    let failLibrary = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (url === `${BASE}/api/v1/workspaces`) {
        return json({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`) {
        if (failLibrary) {
          return json(
            {
              error_code: "internal_error",
              message: "Library backend unavailable",
              request_id: "",
              warnings: [],
            },
            500,
          );
        }
        return json({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`) {
        return json({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return json({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("The server could not complete the request. Please try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading Setup")).not.toBeInTheDocument();

    failLibrary = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("heading", { name: "Setup" }),
    ).toBeInTheDocument();
    expect(screen.getByText("0 of 2 · Pending")).toBeInTheDocument();
    expect(
      screen.getByText("Library is empty. Start by adding a source."),
    ).toBeInTheDocument();
  });

  it("does not call the settings policies endpoint", async () => {
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("1 of 2 · Partial"),
    ).toBeInTheDocument();
    const requestedUrls = fetchMock.mock.calls.map((call) => call[0].toString());
    expect(requestedUrls).not.toContain(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`,
    );
  });

  it("does not repeat the ready-count in the callout body (M-13, 260908 UI audit)", async () => {
    // The header status pill above ("1 of 2 · Partial") already states the
    // count; the callout used to restate it verbatim right underneath.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
      // P9.1 renamed this route family; the audit branch this test came from
      // predates that cutover, and an unmatched mock made SetupScreen render
      // its error state so the status pill never appeared.
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    await screen.findByText("1 of 2 · Partial");
    expect(screen.getByText("Keep going.")).toBeInTheDocument();
    expect(screen.queryByText(/of 3 ready/)).not.toBeInTheDocument();
  });

  it("finishes setup with sources and knowledge pages while Agent is hidden", async () => {
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [{ id: "context-1" }],
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("2 of 2 · Ready")).toBeInTheDocument();
    expect(screen.getByText("Your workspace has sources and knowledge pages.")).toBeInTheDocument();
    expect(screen.queryByText("AI rules")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open Agent|Add rule/ })).not.toBeInTheDocument();
    expect(document.querySelector('a[href="/workspaces/00000000-0000-4000-8000-000000000001/agent"]')).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/workspaces/00000000-0000-4000-8000-000000000001/agent/pages"))).toBe(false);
  });

  it("offers the Agent setup step after the Agent menu is enabled", async () => {
    labs.agent = true;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [{ id: "context-1" }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/setup"]}>
        <SetupScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("2 of 3 · Partial")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add rule" })).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/agent");
    expect(screen.getByText("Add an AI rule to control how answers are written.")).toBeInTheDocument();
  });
});

function mockFetch(responses: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}
