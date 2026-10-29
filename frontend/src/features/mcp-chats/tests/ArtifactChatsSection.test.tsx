import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { McpChatProvenanceEntry } from "../../../shared/api/operations";
import { i18n } from "../../../shared/i18n";
import { ArtifactChatsSection } from "../ArtifactChatsSection";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const PROVENANCE_URL = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/resource-provenance?kind=context_page&id=page-1`;

describe("ArtifactChatsSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("renders provenance entries as chat links", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [PROVENANCE_URL]: { data: { items: [provenanceEntry()] } },
    });

    renderSection();

    const link = await screen.findByRole("link", {
      name: /Codex Research Session/,
    });
    expect(link).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/chats/chat-1");
    expect(
      screen.queryByText("Not yet referenced in any external chat."),
    ).toBeNull();
  });

  it("shows reads before muted search-match provenance", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [PROVENANCE_URL]: {
        data: {
          items: [
            provenanceEntry({ id: "chat-read", title: "Read Session" }),
            {
              ...provenanceEntry({
                id: "chat-search",
                title: "Search Session",
              }),
              verb: "search_match",
            },
          ],
        },
      },
    });

    renderSection();

    const links = await screen.findAllByRole("link");
    expect(links[0]).toHaveTextContent("Referenced");
    expect(links[1]).toHaveTextContent("Search match");
    expect(within(links[1]).getByText("Search match")).toHaveClass(
      "status-pill-muted",
    );
  });

  it("expands past the first 5 chats on click, not just in the header count (UI-17)", async () => {
    const user = userEvent.setup();
    const entries = Array.from({ length: 8 }, (_, i) =>
      provenanceEntry({
        id: `chat-${i + 1}`,
        title: `Session ${i + 1}`,
      }),
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [PROVENANCE_URL]: { data: { items: entries } },
    });

    renderSection();

    await screen.findByRole("link", { name: /Session 1/ });
    expect(screen.getAllByRole("link")).toHaveLength(5);
    expect(screen.queryByRole("link", { name: /Session 6/ })).toBeNull();
    const showAll = screen.getByRole("button", { name: "Show all 8" });

    await user.click(showAll);

    expect(await screen.findByRole("link", { name: /Session 6/ })).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(8);
    expect(screen.queryByRole("button", { name: "Show all 8" })).toBeNull();
  });

  it("does not render a show-all button at or under 5 chats", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [PROVENANCE_URL]: { data: { items: [provenanceEntry()] } },
    });

    renderSection();

    await screen.findByRole("link", { name: /Codex Research Session/ });
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("shows the empty message only after a successful empty load", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [PROVENANCE_URL]: { data: { items: [] } },
    });

    renderSection();

    expect(
      await screen.findByText("No chat has touched this item yet."),
    ).toBeInTheDocument();
  });

  it("shows a retryable error instead of claiming the artifact is unreferenced", async () => {
    const user = userEvent.setup();
    let provenanceCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (url === PROVENANCE_URL) {
        provenanceCalls += 1;
        if (provenanceCalls === 1) {
          return new Response(
            JSON.stringify({
              error_code: "internal_error",
              message: "Provenance unavailable",
              request_id: "req-test",
              warnings: [],
            }),
            { status: 500, headers: { "Content-Type": "application/json" } },
          );
        }
        return jsonResponse({ data: { items: [provenanceEntry()] } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    renderSection();

    expect(
      await screen.findByText(
        "The server could not complete the request. Please try again.",
      ),
    ).toBeInTheDocument();
    // The load failed, so the affirmative empty claim must not render.
    expect(
      screen.queryByText("Not yet referenced in any external chat."),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Provenance unavailable")).toBeNull();
    expect(provenanceCalls).toBe(2);
  });

  it("localizes the heading, empty state, and retry button in Korean", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockFetch({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [PROVENANCE_URL]: { data: { items: [] } },
      });

      renderSection();

      expect(
        await screen.findByText("아직 이 항목을 다룬 채팅이 없습니다."),
      ).toBeInTheDocument();
      expect(
        screen.queryByText("No chat has touched this item yet."),
      ).toBeNull();
      expect(screen.getByText("채팅")).toBeInTheDocument();
      expect(screen.queryByText("Chats")).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });
});

function renderSection() {
  return render(
    <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000001/chats"]}>
      <ArtifactChatsSection resourceKind="context_page" resourceId="page-1" />
    </MemoryRouter>,
  );
}

function mockFetch(responses: Record<string, unknown>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    return jsonResponse(body);
  });
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function workspaceResponse() {
  return {
    data: {
      current_workspace_id: WORKSPACE_ID,
      workspaces: [
        {
          id: WORKSPACE_ID,
          slug: "default",
          title: "Default",
          name: "Default",
          goal: "Test",
          default_language: "en",
          visibility: "private",
        },
      ],
    },
  };
}

function provenanceEntry(
  overrides: { id?: string; title?: string } = {},
): McpChatProvenanceEntry {
  const id = overrides.id ?? "chat-1";
  const title = overrides.title ?? "Codex Research Session";
  return {
    verb: "referenced",
    occurrence_count: 2,
    last_occurred_at: "2026-05-27T09:02:00Z",
    chat_session: {
      id,
      workspace_id: WORKSPACE_ID,
      client: {
        id: "client-1",
        name: "Codex CLI",
        client_type: "codex",
        brand_hint: "codex",
        scopes: ["mcp:read"],
      },
      mcp_session_id: `session-${id}`,
      title,
      summary: "Search and read activity",
      started_at: "2026-05-27T09:00:00Z",
      last_activity_at: "2026-05-27T09:03:00Z",
      tool_call_count: 3,
      write_count: 1,
      error_count: 0,
      failed_count: 0,
      rejected_count: 0,
      archived_at: null,
      title_user_edited: true,
      title_source: "user",
      action: "search",
      // chatPresentationLine renders `subject`, not `title`, when
      // title_source is "user" -- both must move together for the rendered
      // link text to actually vary between entries.
      subject: title,
      subject_kind: null,
      primary_tool_name: "search",
      linked_artifacts: [],
    },
  };
}
