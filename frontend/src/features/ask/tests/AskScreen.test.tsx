import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createContext, useContext } from "react";
import { Link, MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AskTurnDetail } from "../../../shared/api/operations";
import type { TreeNodePayload } from "../../../shared/api/workspace";
import { readAppStyles } from "../../../test-support/appStyles";
import { AskScreen } from "../AskScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-4000-8000-000000000001";
const SESSION_ID = "session-1";
const TURN_ID = "turn-1";
const PARENT_ID = "context-parent-1";

// AskScreen reads role via useOptionalWorkspaceContext to gate write
// affordances; the tests render it without a provider, so mock the role here.
const { askWorkspace } = vi.hoisted(() => ({
  askWorkspace: {
    current: { selectedWorkspace: { role: "owner" } } as Record<string, unknown>,
  },
}));

const DummyWorkspaceCtx = createContext<unknown>(null);

vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../shared/workspace/WorkspaceContext")>();
  return {
    ...actual,
    // Keep real React hook semantics (calls useContext) so a Rules-of-Hooks
    // violation in AskScreen — e.g. calling this after an early return — is
    // surfaced as a hooks-order warning rather than masked by a plain function.
    useOptionalWorkspaceContext: () => {
      useContext(DummyWorkspaceCtx);
      return askWorkspace.current;
    },
  };
});

describe("AskScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.removeItem("dtxwiki:workspace-layout:ask:inspector");
    askWorkspace.current = { selectedWorkspace: { role: "owner" } };
  });

  it("keeps the composer in the content flow without a synthetic spacer", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".ask-screen .feature-main");
    expect(styles).toContain(".ask-content-region");
    expect(styles).toContain("margin-top: auto;");
    expect(styles).not.toContain(".ask-composer-reserve");
  });


  it("refreshes the session list from its menu without replacing the composer or selected session", async () => {
    let sessionReads = 0;
    let detailReads = 0;
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        sessionReads += 1;
        return { data: {
          items: sessionReads === 1 ? [sessionRef()] : [
            { ...sessionRef(), id: "new-session", title: "New Ask Probe" }, sessionRef(),
          ], date_buckets: [], next_cursor: null,
        } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        detailReads += 1;
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <Link to={`/workspaces/${WORKSPACE_ID}/ask`}>Main menu Ask</Link>
        <AskScreen />
      </MemoryRouter>,
    );
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await userEvent.type(composer, "Unsent local question");
    await userEvent.click(screen.getByRole("link", { name: "Main menu Ask" }));
    await screen.findByText("New Ask Probe");
    expect(composer).toHaveValue("Unsent local question");
    expect(sessionReads).toBe(2);
    expect(detailReads).toBe(1);
  });

  it("renders turns and accepts a placement proposal", async () => {
    let promotedBody: unknown = null;
    let acceptedBody: unknown = null;
    // The default title is generated from a multi-line question: one line,
    // at most 60 code points, never a split surrogate pair.
    const question = `Needle\n${"x".repeat(51)}\u{1F600} tail`;
    const title = `Needle ${"x".repeat(51)}\u{1F600}`;
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [
            treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures"),
          ],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail([askTurn({ user_message: question })]) };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/turns/${TURN_ID}/promote`) {
        promotedBody = JSON.parse(String(init?.body));
        return { data: proposal() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/accept`) {
        acceptedBody = JSON.parse(String(init?.body));
        return {
          data: {
            proposal_id: "proposal-1",
            change_set_id: "change-1",
            context_page_id: "00000000-0000-4000-8000-000000000107",
            context_page_version_id: "version-1",
            tree_node_id: "tree-1",
            feedback_item_id: null,
            review_item_id: null,
            review_required_reason: null,
            audit_event_id: "audit-1",
          },
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    // The inspector starts collapsed on Ask (`defaultInspectorOpen={false}`)
    // and auto-opens when a turn is saved, so its contents are asserted after
    // that click rather than before it.
    expect(screen.queryByText(PARENT_ID)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    expect(screen.getByText("confidence medium")).toBeInTheDocument();
    const pageType = screen.getByRole("combobox", { name: "Page type" });
    expect(pageType.querySelectorAll("option")).toHaveLength(10);
    await userEvent.selectOptions(pageType, "comparison");
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    expect(await screen.findByText("Ask turn 1")).toBeInTheDocument();
    // The promotion-state pill renders the translated label, not the raw
    // "not_promoted" enum value (C4 domainLabel routing).
    expect(screen.getByText("Not promoted")).toBeInTheDocument();
    expect(screen.queryByText("not_promoted")).not.toBeInTheDocument();
    expect(screen.getByText("1 alternatives")).toBeInTheDocument();
    expect(screen.getByText("Destination")).toBeInTheDocument();
    expect(screen.getAllByText("Ask Captures").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Save to new Context page").length)
      .toBeGreaterThan(0);
    expect(screen.getByText("Context-first retrieval summary")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept placement" }));

    expect(promotedBody).toEqual({
      save_mode: "create_page",
      parent_node_id: PARENT_ID,
      page_type: "comparison",
      title,
      body_markdown: null,
      alternatives: [],
    });
    expect(acceptedBody).toEqual({
      final_save_mode: "create_page",
      final_parent_node_id: PARENT_ID,
      final_page_type: "comparison",
      final_title: title,
      target_page_id: null,
      review_required: false,
    });
    expect(await screen.findByText("Saved to Context")).toBeInTheDocument();
  });

  it("hides the Details rail when no Ask session is selected", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => {
      const layout = view.container.querySelector(".workspace-screen");
      expect(layout).toHaveStyle({ "--workspace-right-w": "0px" });
    });
    expect(
      view.container.querySelector(".workspace-panel-restore-right"),
    ).toBeNull();
  });

  it("renders actor-visible graph context as linked inspector documents", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return {
          data: sessionDetail([
            askTurn({
              graph_context: [
                {
                  kind: "context_page",
                  id: "00000000-0000-4000-8000-000000000102",
                  title: "Linked Context",
                  path: "/context/linked-context.md",
                  edge_kind: "wikilink",
                  direction: "out",
                },
              ],
            }),
          ]),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    expect(screen.getByText("Linked documents")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Linked Context/ }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/00000000-0000-4000-8000-000000000102`);
  });

  it("renders the assistant answer as markdown instead of raw syntax (M-9)", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return {
          data: sessionDetail([
            askTurn({
              assistant_message:
                "## Invoice cycle\n\n| Stage | Owner |\n| --- | --- |\n| Metering | platform |\n\n[Confirmed](/context/confirmed.md) [Unconfirmed](/context/unconfirmed.md)",
              link_targets: [{
                locator: "/context/confirmed.md",
                href: `/p/${WORKSPACE_ID}/22222222-2222-4222-8222-222222222222`,
                target_page_id: "22222222-2222-4222-8222-222222222222",
                state: "ok",
              }],
            }),
          ]),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Invoice cycle" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Stage" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Metering" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Confirmed" })).toHaveAttribute("href", `/p/${WORKSPACE_ID}/22222222-2222-4222-8222-222222222222`);
    expect(screen.getByText("Unconfirmed").closest("a")).toBeNull();
    expect(
      screen.queryByText(/\| Stage \| Owner \|/),
    ).not.toBeInTheDocument();
  });

  it("submits selected save mode and can reject a proposal", async () => {
    let promotedBody: unknown = null;
    let acceptedBody: unknown = null;
    let rejectedBody: unknown = null;
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [
            treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures"),
            contextPageNode("context-node-1", "00000000-0000-4000-8000-000000000103", "Existing Page"),
          ],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/turns/${TURN_ID}/promote`) {
        promotedBody = JSON.parse(String(init?.body));
        return {
          data: {
            ...proposal(),
            proposed_save_mode: "append_section",
            placement_confidence: "low",
            alternatives: [{ body_markdown: "candidate" }],
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/accept`) {
        acceptedBody = JSON.parse(String(init?.body));
        return {
          data: {
            proposal_id: "proposal-1",
            change_set_id: "change-1",
            context_page_id: "00000000-0000-4000-8000-000000000103",
            context_page_version_id: "version-1",
            tree_node_id: null,
            feedback_item_id: null,
            review_item_id: "review-1",
            review_required_reason: "low confidence",
            audit_event_id: "audit-1",
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/reject`) {
        rejectedBody = JSON.parse(String(init?.body));
        return { data: { ...proposal(), status: "rejected" } };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await userEvent.selectOptions(screen.getByLabelText("Save mode"), "append_section");
    await userEvent.selectOptions(screen.getByLabelText("Target page"), "00000000-0000-4000-8000-000000000103");
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    expect(await screen.findByText("confidence low")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept placement" }));

    expect(promotedBody).toMatchObject({
      save_mode: "append_section",
      parent_node_id: PARENT_ID,
      page_type: "guide",
      title: "Needle",
    });
    expect(acceptedBody).toEqual({
      final_save_mode: "append_section",
      final_parent_node_id: PARENT_ID,
      final_page_type: "guide",
      final_title: "Needle",
      target_page_id: "00000000-0000-4000-8000-000000000103",
      review_required: true,
    });
    expect(await screen.findByText("Saved for review")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    await userEvent.type(screen.getByLabelText("Reject reason"), "Wrong target");
    await userEvent.click(screen.getByRole("button", { name: "Reject placement" }));

    expect(rejectedBody).toEqual({ reason: "Wrong target" });
    expect(await screen.findByText("Placement rejected")).toBeInTheDocument();
  });

  it("resets the placement panel state when saving a different turn", async () => {
    let turns = [askTurn()];
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns` &&
        init?.method === "POST"
      ) {
        turns = [
          askTurn(),
          askTurn({
            id: "turn-2",
            turn_no: 2,
            user_message: "Follow up question",
            assistant_message: "Context-first retrieval summary:\n- More evidence",
          }),
        ];
        return { data: turns[1] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail(turns) };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    expect(screen.getByLabelText("Title")).toHaveValue("Needle");

    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Follow up question",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Turn 2");

    // Saving the newer turn must not inherit turn 1's title/proposal state.
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await waitFor(() => {
      expect(screen.getByLabelText("Title")).toHaveValue("Follow up question");
    });
  });

  it("appends an older keyset page when Load more is pressed", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [sessionRef()],
            date_buckets: [],
            next_cursor: "cursor-2",
            has_more: true,
          },
        };
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions?cursor=cursor-2`
      ) {
        return {
          data: {
            items: [olderSessionRef()],
            date_buckets: [],
            next_cursor: null,
            has_more: false,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    // First page only: the older session is not loaded yet.
    await screen.findByText(/Context-first retrieval summary/);
    expect(screen.getByText("1+ items")).toHaveTextContent(/^1\+\s*items$/);
    expect(
      screen.queryByRole("button", { name: /Second Ask/ }),
    ).not.toBeInTheDocument();

    await userEvent.click(
      await screen.findByRole("button", { name: "Load more" }),
    );

    // The cursor page is appended and the exhausted control disappears.
    expect(
      await screen.findByRole("button", { name: /Second Ask/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("2 items")).toHaveTextContent(/^2\s*items$/);
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("names the coverage gap the same way the Lint screen does in Korean", async () => {
    // Ask warns about the gap, Lint is where the user goes to find it: the two
    // screens must not call the same record different things.
    let resolveTurn!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "ko" }],
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns` &&
        init?.method === "POST"
      ) {
        return new Promise<Response>((resolve) => {
          resolveTurn = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.type(screen.getByRole("textbox", { name: "메시지" }), "안녕");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));
    resolveTurn(
      jsonResponse({
        data: askTurn({ id: "turn-2", turn_no: 2, user_message: "안녕" }),
        envelope: {
          warnings: [
            {
              code: "coverage_gap_recorded",
              message: "No evidence found; a coverage gap was recorded.",
            },
          ],
        },
      }),
    );

    expect(
      await screen.findByText("답하지 못한 질의를 커버리지 갭으로 기록했습니다."),
    ).toBeInTheDocument();
  });

  it("localizes a non-gap post-create warning banner from the shared catalog", async () => {
    // The transient post-create banner (lastTurnWarnings) must resolve codes
    // through the workspace search catalog like the turn cards do, not fall
    // back to the backend English `message`. coverage_gap_recorded was already
    // special-cased; keyword_lane_unavailable is the code that regressed to
    // English until the shared resolver was applied here too.
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              { id: WORKSPACE_ID, slug: "default", default_language: "ko" },
            ],
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns` &&
        init?.method === "POST"
      ) {
        return {
          data: askTurn({ id: "turn-2", turn_no: 2, user_message: "안녕" }),
          envelope: {
            warnings: [
              {
                code: "keyword_lane_unavailable",
                message:
                  "Tier-B FTS5 symbolic lanes are unavailable for this database; "
                  + "exact/direct lanes remain active.",
              },
              {
                code: "unregistered_backend_warning",
                message: "raw warning req-secret",
              },
            ],
          },
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.type(screen.getByRole("textbox", { name: "메시지" }), "안녕");
    await userEvent.click(screen.getByRole("button", { name: "보내기" }));

    expect(
      await screen.findByText(
        /이 데이터베이스에서는 로컬 FTS5 검색을 사용할 수 없습니다\. 정확 일치와 직접 조회는 계속 동작합니다/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Tier-B FTS5 symbolic lanes are unavailable/),
    ).toBeNull();
    expect(
      screen.getByText(/요청이 경고와 함께 완료되었습니다\./),
    ).toBeInTheDocument();
    expect(screen.queryByText("raw warning req-secret")).toBeNull();
  });

  it("renders search-plane warnings from the shared catalog in Korean", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              { id: WORKSPACE_ID, slug: "default", default_language: "ko" },
            ],
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return {
          data: sessionDetail([
            askTurn({ warning_codes: ["keyword_lane_unavailable"] }),
          ]),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "이 데이터베이스에서는 로컬 FTS5 검색을 사용할 수 없습니다. 정확 일치와 직접 조회는 계속 동작합니다",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("keyword lane unavailable")).toBeNull();
  });

  it("discards a stale Load more page when the list reloads mid-flight", async () => {
    let resolveOlder!: (response: Response) => void;
    let firstPageGets = 0;
    // Earlier activity than session-1 so session-1 sorts first and is the
    // auto-selected row; clicking this one is then an unambiguous switch.
    const middleRef = {
      ...sessionRef(),
      id: "session-2",
      title: "Middle session",
      started_at: "2026-05-08T00:00:00Z",
      updated_at: "2026-05-08T00:00:00Z",
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const method = init?.method ?? "GET";
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions` &&
        method === "GET"
      ) {
        firstPageGets += 1;
        // The reload after selecting another session returns a fresh first page
        // with a *different* cursor, so the parked older page is now stale.
        return jsonResponse({
          data: {
            items: [sessionRef(), middleRef],
            date_buckets: [],
            next_cursor: firstPageGets === 1 ? "cursor-2" : "cursor-3",
            has_more: true,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions?cursor=cursor-2`) {
        // Park the older page until after the reload has replaced the list.
        return new Promise<Response>((resolve) => {
          resolveOlder = resolve;
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        return jsonResponse({
          data: { ...sessionDetail(), id: "session-2", title: "Middle session" },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    // Park the Load more older-page request, then reload the list by selecting
    // another session before that older page resolves.
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await userEvent.click(screen.getByRole("button", { name: /Middle session/ }));
    // Wait until the reload has fully applied (cursor-3 first page, session-2 now
    // the active row) so the two setStates do not race in the test itself.
    await waitFor(() => {
      expect(firstPageGets).toBe(2);
      expect(screen.getByRole("button", { name: /Middle session/ })).toHaveAttribute(
        "aria-current",
        "page",
      );
    });

    // Release the now-stale older page; the cursor guard must drop it.
    resolveOlder(
      jsonResponse({
        data: {
          items: [{ ...sessionRef(), id: "session-3", title: "Stale session" }],
          date_buckets: [],
          next_cursor: null,
          has_more: false,
        },
      }),
    );

    // findByRole waits for loadingMore to reset (proving the older page resolved)
    // AND requires the control to still be present — i.e. the guard kept the
    // fresh has_more=true cursor instead of the stale page's null/has_more=false.
    await screen.findByRole("button", { name: "Load more" });
    // The stale "Stale session" page never grafts onto the fresh list.
    expect(screen.queryByRole("button", { name: /Stale session/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Middle session/ })).toBeInTheDocument();
  });

  it("disables session switching while a turn is sending", async () => {
    let resolveTurn!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: {
            items: [sessionRef(), olderSessionRef()],
            date_buckets: [],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        return jsonResponse({
          data: { ...sessionDetail(), id: "session-2", title: "Second Ask" },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns` &&
        init?.method === "POST"
      ) {
        return new Promise<Response>((resolve) => {
          resolveTurn = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    // The rail stays locked until the turn settles so the trailing load()
    // cannot clobber a competing session selection.
    expect(screen.getByRole("button", { name: /Second Ask/ })).toBeDisabled();

    resolveTurn(
      jsonResponse({
        data: askTurn({ id: "turn-2", turn_no: 2, user_message: "Hello" }),
        envelope: {
          warnings: [
            {
              code: "coverage_gap_recorded",
              message: "No evidence found; a coverage gap was recorded.",
            },
          ],
        },
      }),
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Second Ask/ })).toBeEnabled();
    });
    expect(
      screen.getByText("This unanswered query was recorded as a coverage gap."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Second Ask/ }));
    await waitFor(() => {
      expect(
        screen.queryByText(
          "This unanswered query was recorded as a coverage gap.",
        ),
      ).not.toBeInTheDocument();
    });
  });

  it("keeps a newer route when a deferred turn completes", async () => {
    let resolveTurn!: (response: Response) => void;
    let resolveSecondDetail!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: { items: [sessionRef(), olderSessionRef()], date_buckets: [], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({ data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        return new Promise<Response>((resolve) => {
          resolveSecondDetail = resolve;
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns` &&
        init?.method === "POST"
      ) {
        return new Promise<Response>((resolve) => {
          resolveTurn = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    let pathname = "";
    function RouteControls() {
      const navigate = useNavigate();
      pathname = useLocation().pathname;
      return (
        <button
          type="button"
          onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/ask/session-2`)}
        >
          Navigate to second Ask
        </button>
      );
    }

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
        <RouteControls />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await userEvent.click(screen.getByRole("button", { name: "Navigate to second Ask" }));
    await waitFor(() => {
      expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/session-2`);
    });
    expect(screen.getByRole("status", { name: "Loading Ask" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save answer" })).toBeNull();

    resolveSecondDetail(jsonResponse({
      data: { ...sessionDetail(), id: "session-2", title: "Second Ask", turns: [] },
    }));
    await screen.findByRole("heading", { name: "Second Ask" });

    resolveTurn(jsonResponse({ data: askTurn({ id: "turn-2", turn_no: 2 }) }));
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Second Ask" })).toBeInTheDocument();
    });
    expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/session-2`);
  });

  it("starts a fresh session when sending during an overview reload", async () => {
    let sessionListCalls = 0;
    let resolveOverviewSessions!: (response: Response) => void;
    const newSession = { ...sessionDetail([]), id: "session-new", title: "New Ask" };
    let createTurnPath = "";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        if (init?.method === "POST") return jsonResponse({ data: newSession });
        sessionListCalls += 1;
        if (sessionListCalls === 2) {
          return new Promise<Response>((resolve) => { resolveOverviewSessions = resolve; });
        }
        return jsonResponse({ data: { items: [sessionRef()], date_buckets: [], next_cursor: null } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-new`) {
        return jsonResponse({ data: newSession });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/`) &&
          url.endsWith("/turns") && init?.method === "POST") {
        createTurnPath = url;
        return jsonResponse({ data: askTurn({ id: "turn-new", ask_session_id: "session-new" }) });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    let pathname = "";
    function RouteControls() {
      const navigate = useNavigate();
      pathname = useLocation().pathname;
      return (
        <button type="button" onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/ask`)}>
          Open Ask overview
        </button>
      );
    }
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
        <RouteControls />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: "Open Ask overview" }));
    await waitFor(() => expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask`));
    const message = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(message, "Fresh route target");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(createTurnPath).toContain("/sessions/session-new/turns"));
    expect(createTurnPath).not.toContain(`/sessions/${SESSION_ID}/turns`);
    expect(await screen.findByRole("heading", { name: "New Ask" })).toBeInTheDocument();
    resolveOverviewSessions(jsonResponse({
      data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
    }));
  });

  it("disables the composer while another session is loading", async () => {
    let resolveDetail!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: {
            items: [sessionRef(), olderSessionRef()],
            date_buckets: [],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        return new Promise<Response>((resolve) => {
          resolveDetail = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: /Second Ask/ }));

    expect(screen.getByRole("status", { name: "Loading Ask" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save answer" })).toBeNull();

    resolveDetail(
      jsonResponse({
        data: { ...sessionDetail(), id: "session-2", title: "Second Ask" },
      }),
    );
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    });
  });

  it("uses localized fallback copy for an unexpected send-turn failure", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return new Response(
          JSON.stringify({
            data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] },
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return new Response(
          JSON.stringify({
            data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return new Response(
          JSON.stringify({ data: [treeNode("ask-root", "ask", "Ask", null)] }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return new Response(
          JSON.stringify({
            data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return new Response(JSON.stringify({ data: sessionDetail() }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}/turns`
      ) {
        throw new Error("raw req-secret");
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(input, "Hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByText("Failed to process the request."),
    ).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
  });

  it("creates a new session when the user starts a new ask", async () => {
    let created = false;
    let createCount = 0;
    const createdSession = {
      ...sessionDetail(),
      id: "session-new",
      title: "Created Ask",
      turn_count: 0,
      placement_tree_node_id: "ask-created",
      turns: [],
    };
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions` &&
        init?.method === "POST"
      ) {
        createCount += 1;
        created = true;
        return { data: createdSession };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`
      ) {
        if (!created) {
          return { data: { items: [], date_buckets: [], next_cursor: null } };
        }
        return {
          data: {
            items: [
              {
                ...sessionRef(),
                id: "session-new",
                title: "Created Ask",
                turn_count: 0,
              },
            ],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-new`
      ) {
        return { data: createdSession };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("button", { name: "New" });
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(await screen.findAllByText("Created Ask")).not.toHaveLength(0);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New" })).toHaveAttribute(
        "aria-busy",
        "false",
      ),
    );
    expect(createCount).toBe(1);
  });

  it("locks session selection and the composer while creating a session", async () => {
    let resolveCreate!: (response: Response) => void;
    let createStarted = false;
    let secondDetailRequests = 0;
    const createdSession = {
      ...sessionDetail(),
      id: "session-new",
      title: "Created Ask",
      turn_count: 0,
      turns: [],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions` &&
        init?.method === "POST"
      ) {
        createStarted = true;
        return new Promise<Response>((resolve) => {
          resolveCreate = resolve;
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: {
            items: createStarted
              ? [{ ...sessionRef(), id: "session-new", title: "Created Ask" }]
              : [sessionRef(), olderSessionRef()],
            date_buckets: [],
            next_cursor: null,
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        secondDetailRequests += 1;
        return jsonResponse({
          data: { ...sessionDetail(), id: "session-2", title: "Second Ask" },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-new`) {
        return jsonResponse({ data: createdSession });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    const newButton = screen.getByRole("button", { name: "New" });
    await userEvent.click(newButton);

    expect(newButton).toBeDisabled();
    expect(screen.getByRole("button", { name: /Second Ask/ })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /Second Ask/ }));
    expect(secondDetailRequests).toBe(0);

    resolveCreate(jsonResponse({ data: createdSession }));
    expect(await screen.findAllByText("Created Ask")).not.toHaveLength(0);
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    });
  });

  it("hides composer, create, and save affordances for a viewer", async () => {
    askWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [
            treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures"),
          ],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    // existing sessions and turns stay browsable
    expect(screen.getByLabelText("Ask history")).toHaveTextContent("Research Ask");
    // every write affordance is hidden for viewers
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save answer" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });

  it("does not auto-create a session for a viewer on the new-ask route", async () => {
    askWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    let createCount = 0;
    mockFetch((url, init) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions` &&
        init?.method === "POST"
      ) {
        createCount += 1;
        return { data: sessionDetail() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Ask history yet" }))
      .toBeInTheDocument();
    // empty state keeps read-only workflow links but no create button
    expect(screen.getAllByRole("link", { name: "Open Library" })[0])
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library`);
    expect(screen.queryByRole("button", { name: "New" })).toBeNull();
    expect(createCount).toBe(0);
  });

  it("shows workflow actions when no ask sessions exist", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Ask history yet" }))
      .toBeInTheDocument();
    // The rail keeps a short empty label so the main headline isn't duplicated,
    // and the empty-state primary describes the action instead of just "New".
    expect(screen.getAllByText("No Ask history yet")).toHaveLength(1);
    expect(screen.getByLabelText("Ask history")).toHaveTextContent(
      "No sessions yet",
    );
    const setup = screen.getByLabelText("Workspace setup sequence");
    expect(setup).toHaveTextContent("Write Context page");
    expect(setup).toHaveTextContent("Save or review");
    expect(setup).not.toHaveTextContent("Ask a question");
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "Start first Ask session" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "New" })).toHaveLength(1);
    expect(screen.getAllByRole("link", { name: "Open Library" })[0])
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library`);
    expect(screen.getAllByRole("link", { name: "Write Context page" })[0])
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/new`);
  });

  it("shows next-step actions when a session has no turns", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [{ ...sessionRef(), turn_count: 0 }],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: { ...sessionDetail([]), turn_count: 0 } };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No turns yet" }))
      .toBeInTheDocument();
    // The session already exists, so the workspace first-use checklist (which
    // still lists "Ask a question" as a pending step) has no place here.
    expect(screen.queryByLabelText("Workspace setup sequence")).toBeNull();
    expect(screen.getByRole("link", { name: "Open Library" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library`);
    expect(screen.getByRole("link", { name: "Write Context page" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/new`);
  });

  it("links no-evidence answers to source, context, and search recovery", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return {
          data: sessionDetail([
            askTurn({
              assistant_message: "No matching Context or Library evidence was found.",
              cited_context_page_ids: [],
              cited_library_artifact_ids: [],
              fallback_disclosure: true,
            }),
          ]),
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("No matching Context or Library evidence was found."))
      .toBeInTheDocument();
    expect(screen.getByLabelText("No evidence actions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save answer" })).toBeDisabled();
    expect(screen.getByText(/saving is disabled/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add source material" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library`);
    expect(screen.getByRole("link", { name: "Write Context page" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/new`);
    expect(screen.getByRole("link", { name: "Search workspace" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/search?q=Needle`);
  });

  it("renders persisted evidence identity, search path, and library fallback", async () => {
    const evidenceTurn = askTurn({
      assistant_message: "No matching Context or Library evidence was found.",
      cited_context_page_ids: ["00000000-0000-4000-8000-000000000103"],
      retrieval_trace: [
        {
          id: "00000000-0000-4000-8000-000000000103",
          type: "context_page",
          title: "Deployment Guide",
          score: "1.00",
          match_reason: "exact_title",
          concept_path: "/context/deployment-guide.md",
        },
      ],
      tried_lanes: ["entity", "library_fallback"],
      fallback_disclosure: true,
    });
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail([evidenceTurn]) };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Cited evidence" }))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Deployment Guide" }))
      .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/00000000-0000-4000-8000-000000000103`);
    expect(screen.getAllByText("Exact title")).toHaveLength(2);
    // M-11 (260908 UI audit): the ".md" suffix is a backend identifier
    // (concept_path), not a real file the user can open -- it must not leak.
    expect(screen.getByText("/context/deployment-guide")).toBeInTheDocument();
    expect(
      screen.queryByText("/context/deployment-guide.md"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Search path")).toBeInTheDocument();
    expect(
      screen.getByText("Context had no match, so Library evidence was used."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("No evidence actions")).toBeNull();
    expect(screen.getByRole("button", { name: "Save answer" })).toBeEnabled();
  });

  it("renders ask sessions as a newest-first rail with localized bucket separators", async () => {
    // Drive the bucket headers off "now" so the Today/Yesterday grouping is
    // deterministic regardless of when the suite runs (the shared time bucket
    // is calendar-day relative, not a frozen absolute date).
    const todayAt = new Date();
    todayAt.setHours(9, 0, 0, 0);
    const yesterdayAt = new Date(todayAt);
    yesterdayAt.setDate(yesterdayAt.getDate() - 1);
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return {
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              {
                id: WORKSPACE_ID,
                slug: "default",
                title: "Default",
                name: "Default",
                goal: "Build a source-grounded workspace for cited answers, reusable notes, and team knowledge.",
                default_language: "ko",
                visibility: "private",
                role: "owner",
              },
            ],
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [
              {
                ...sessionRef(),
                id: "older-session",
                title: "Older Ask",
                updated_at: yesterdayAt.toISOString(),
              },
              { ...sessionRef(), updated_at: todayAt.toISOString() },
            ],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [
            treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures"),
          ],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("treeitem")).not.toBeInTheDocument();
    const localizedRail = screen.getByLabelText("질문 히스토리");
    expect(localizedRail).toHaveTextContent("히스토리");
    // Shared bucket headers replace the per-day long dates.
    expect(localizedRail).toHaveTextContent("오늘");
    expect(localizedRail).toHaveTextContent("어제");
    expect(screen.getByRole("textbox", { name: "메시지" })).toBeInTheDocument();
    const saveAnswer = screen.getByRole("button", { name: "답변 저장" });
    expect(saveAnswer).toBeInTheDocument();
    await userEvent.click(saveAnswer);
    const pageType = screen.getByRole("combobox", { name: "페이지 유형" });
    expect(pageType.querySelectorAll("option")).toHaveLength(10);
    expect(pageType).toHaveTextContent("출처");
    expect(pageType).toHaveTextContent("엔터티");
    expect(pageType).toHaveTextContent("모순");
    expect(pageType).toHaveTextContent("쿼리");
    expect(screen.getByRole("button", { name: "보내기" })).toBeInTheDocument();
    const railButtons = screen
      .getAllByRole("button")
      .filter((button) => button.closest(".ask-session-list"));
    expect(railButtons.map((button) => button.textContent)).toEqual([
      expect.stringContaining("Research Ask"),
      expect.stringContaining("Older Ask"),
    ]);
  });

  it("re-derives default-titled sessions from started_at and pluralizes turn counts", async () => {
    const startedAt = new Date();
    startedAt.setHours(9, 0, 0, 0);
    // The server froze a contradictory UTC-stamped default title; the rail must
    // render a locally-derived label, not the stale "New Ask 1999-..." string.
    const frozenServerTitle = "New Ask 1999-12-31 23:59";
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [
              {
                ...sessionRef(),
                title: frozenServerTitle,
                title_user_edited: false,
                started_at: startedAt.toISOString(),
                updated_at: startedAt.toISOString(),
                turn_count: 1,
              },
            ],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    const rail = screen.getByLabelText("Ask history");
    // The frozen UTC string is never shown; the derived label leads with "New Ask · ".
    expect(rail).not.toHaveTextContent(frozenServerTitle);
    expect(rail).toHaveTextContent(/New Ask ·/);
    // F-3: the count badge reads "1 item" (singular noun), not a bare number.
    expect(screen.getByText("1 item")).toHaveTextContent(/^1\s*item$/);
    // One turn is singular, not "1 turns".
    expect(rail).toHaveTextContent("1 turn");
    expect(rail).not.toHaveTextContent("1 turns");
  });

  it("keeps user-edited titles and pluralizes multi-turn counts", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [{ ...sessionRef(), turn_count: 3 }],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    const rail = screen.getByLabelText("Ask history");
    // title_user_edited sessions keep their stored title verbatim.
    expect(rail).toHaveTextContent("Research Ask");
    expect(rail).toHaveTextContent("3 turns");
  });

  it("surfaces accept-placement API failures", async () => {
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [
            treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures"),
          ],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        return { data: sessionDetail() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/turns/${TURN_ID}/promote`) {
        return { data: proposal() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/accept`) {
        return new Response(
          JSON.stringify({
            error_code: "save_failed",
            message: "Cannot save placement",
            request_id: "req-test",
            warnings: [],
          }),
          {
            status: 500,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/Context-first retrieval summary/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    await userEvent.click(screen.getByRole("button", { name: "Accept placement" }));

    expect(
      await screen.findByText(
        "Could not save changes. Try again.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the placement panel mounted until the post-accept refresh resolves", async () => {
    let accepted = false;
    let resolveDetail: ((response: Response) => void) | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return jsonResponse({
          data: { items: [sessionRef()], date_buckets: [], next_cursor: null },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        if (accepted) {
          return new Promise<Response>((resolve) => {
            resolveDetail = resolve;
          });
        }
        return jsonResponse({ data: sessionDetail() });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/turns/${TURN_ID}/promote`) {
        return jsonResponse({ data: proposal() });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/accept`
      ) {
        accepted = true;
        return jsonResponse({ data: acceptResult() });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    await screen.findByText("1 alternatives");
    await userEvent.click(screen.getByRole("button", { name: "Accept placement" }));

    // The session refresh is still in flight: the panel stays mounted and busy
    // instead of unmounting before the refresh settles.
    await waitFor(() => expect(resolveDetail).not.toBeNull());
    expect(screen.getByRole("button", { name: "Saving…" })).toBeInTheDocument();
    expect(screen.queryByText("Saved to Context")).toBeNull();

    resolveDetail!(jsonResponse({ data: sessionDetail() }));

    expect(await screen.findByText("Saved to Context")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Accept placement" })).toBeNull();
    });
  });

  it("surfaces a failed post-accept refresh on the screen banner", async () => {
    let accepted = false;
    let secondSessionAttempts = 0;
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return { data: { items: [sessionRef()], date_buckets: [], next_cursor: null } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return {
          data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")],
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        if (accepted) {
          return new Response(
            JSON.stringify({
              error_code: "service_unavailable",
              message: "Session refresh failed",
              request_id: "req-test",
              warnings: [],
            }),
            {
              status: 500,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return { data: sessionDetail() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        secondSessionAttempts += 1;
        if (secondSessionAttempts === 1) {
          return new Response(
            JSON.stringify({
              error_code: "service_unavailable",
              message: "Second session unavailable",
              request_id: "req-second-test",
              warnings: [],
            }),
            {
              status: 500,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return { data: { ...sessionDetail(), id: "session-2", title: "Second Ask" } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/turns/${TURN_ID}/promote`) {
        return { data: proposal() };
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/ingress-proposals/proposal-1/accept`
      ) {
        accepted = true;
        return { data: acceptResult() };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    let pathname = "";
    function RetryTarget() {
      const navigate = useNavigate();
      pathname = useLocation().pathname;
      return (
        <button
          type="button"
          onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/ask/session-2`)}
        >
          Open second Ask
        </button>
      );
    }

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
        <RetryTarget />
      </MemoryRouter>,
    );

    await screen.findByText(/Context-first retrieval summary/);
    await userEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await userEvent.click(screen.getByRole("button", { name: "Propose placement" }));
    await screen.findByText("1 alternatives");
    await userEvent.click(screen.getByRole("button", { name: "Accept placement" }));

    // The accept itself succeeded, but the refresh failure must reach the
    // screen banner instead of vanishing into the unmounted panel.
    expect(
      await screen.findByText(
        "The service is temporarily unavailable. Try again shortly.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("Saved to Context")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept placement" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Open second Ask" }));
    expect(
      await screen.findByText("The service is temporarily unavailable. Try again shortly."),
    ).toBeInTheDocument();
    expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/session-2`);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Second Ask" }),
    ).toBeInTheDocument();
    expect(secondSessionAttempts).toBe(2);
    expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/session-2`);
  });

  it("settles on the session you clicked last instead of ping-ponging", async () => {
    // Going back to a session you already visited used to leave the loaded
    // session and the URL disagreeing for a moment, and the URL effect read
    // that gap as "navigate back" -- so a -> b -> a alternated forever, firing
    // a detail request each way. The URL is the only source of truth now.
    const detailRequests: string[] = [];
    mockFetch((url) => {
      if (url === `${BASE}/api/v1/workspaces`) {
        return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [{ id: WORKSPACE_ID, slug: "default", default_language: "en", role: "owner" }] } };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
        return {
          data: {
            items: [sessionRef(), olderSessionRef()],
            date_buckets: [],
            next_cursor: null,
          },
        };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=ask`) {
        return { data: [treeNode("ask-root", "ask", "Ask", null)] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return { data: [treeNode(PARENT_ID, "context", "Ask Captures", "ask_captures")] };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/${SESSION_ID}`) {
        detailRequests.push(SESSION_ID);
        return { data: sessionDetail() };
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions/session-2`) {
        detailRequests.push("session-2");
        return { data: { ...sessionDetail(), id: "session-2", title: "Second Ask" } };
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    let pathname = "";
    function LocationProbe() {
      pathname = useLocation().pathname;
      return null;
    }

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
        <LocationProbe />
      </MemoryRouter>,
    );

    await screen.findByRole("button", { name: /Second Ask/ }, { timeout: 5000 });
    // The session the initial load resolved gets a URL of its own.
    await waitFor(() => expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`));

    await userEvent.click(screen.getByRole("button", { name: /Second Ask/ }));
    await waitFor(() => expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/session-2`));

    await userEvent.click(screen.getByRole("button", { name: /Research Ask/ }));
    await waitFor(() => expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`));

    // Nothing may move after the click settles: the loop showed up as the URL
    // and the active row alternating on their own, so hold still and watch.
    const settled = detailRequests.length;
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(pathname).toBe(`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`);
    expect(detailRequests.length).toBe(settled);
    expect(detailRequests).toEqual([SESSION_ID, "session-2", SESSION_ID]);
    // Longer than the default: this test deliberately sits still for 150ms to
    // watch for movement, and it is the last of thirty in a loaded file.
  }, 20000);

  // Keep this one last. It leaves a fetch that never resolves, and a test
  // added after it inherits a screen stuck on "Loading Ask".
  it("does not warn about state updates if unmounted while loading", async () => {
    // Stall the workspaces probe forever so the initial async `load()` is
    // still in flight when we unmount the component; without the unmount
    // guard React logs a "state update on unmounted component" warning.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return new Promise<Response>(() => {
          /* never resolves */
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/ask/${SESSION_ID}`]}>
        <AskScreen />
      </MemoryRouter>,
    );

    // Route loading status is rendered immediately.
    expect(screen.getByRole("status", { name: "Loading Ask" })).toBeInTheDocument();
    unmount();

    // Give the still-pending workspaces fetch a chance to settle in the
    // microtask queue; if a state update sneaks through it would warn now.
    await Promise.resolve();
    await Promise.resolve();
    await waitFor(() => {
      const stateWarnings = errorSpy.mock.calls.filter((args) =>
        String(args[0] ?? "").includes("unmounted"),
      );
      expect(stateWarnings).toEqual([]);
    });

    errorSpy.mockRestore();
  });

});

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const body = handler(input.toString(), init);
    if (body instanceof Response) {
      return body;
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sessionRef() {
  return {
    id: SESSION_ID,
    title: "Research Ask",
    started_at: "2026-05-09T00:00:00Z",
    updated_at: "2026-05-09T00:00:00Z",
    actor_kind: "user",
    title_user_edited: true,
    topic_tags: [],
    turn_count: 1,
  };
}

function olderSessionRef() {
  return {
    ...sessionRef(),
    id: "session-2",
    title: "Second Ask",
    started_at: "2026-05-08T00:00:00Z",
    updated_at: "2026-05-08T00:00:00Z",
  };
}

function sessionDetail(turns: AskTurnDetail[] = [askTurn()]) {
  return {
    ...sessionRef(),
    workspace_id: WORKSPACE_ID,
    ended_at: null,
    actor_id: null,
    default_context_parent_tree_node_id: PARENT_ID,
    placement_tree_node_id: "ask-root",
    tree_path: [],
    turns,
  };
}

function askTurn(overrides: Partial<AskTurnDetail> = {}): AskTurnDetail {
  return {
    id: TURN_ID,
    ask_session_id: SESSION_ID,
    turn_no: 1,
    user_message: "Needle",
    assistant_message: "Context-first retrieval summary:\n- Evidence",
    cited_context_page_ids: ["00000000-0000-4000-8000-000000000103"],
    cited_library_artifact_ids: [],
    topic_tags: [],
    promoted_context_page_id: null,
    context_ingress_proposal_ids: [],
    recommended_placement_tree_node_id: PARENT_ID,
    placement_confidence: "medium",
    promotion_state: "not_promoted",
    created_at: "2026-05-09T00:00:00Z",
    retrieval_trace: [],
    tried_lanes: [],
    fallback_disclosure: false,
    graph_context: [],
    ...overrides,
  };
}

function proposal() {
  return {
    id: "proposal-1",
    workspace_id: WORKSPACE_ID,
    source_kind: "ask_turn",
    source_ref: { kind: "ask_turn", id: TURN_ID },
    source_item_ids: [],
    proposed_context_page_type: "guide",
    proposed_parent_tree_node_id: PARENT_ID,
    proposed_parent_breadcrumb: [],
    proposed_title: "Needle",
    proposed_save_mode: "create_page",
    placement_reason: "Ask turn 1",
    placement_confidence: "medium",
    alternatives: [{ body_markdown: "Context-first retrieval summary" }],
    status: "proposed",
    accepted_change_set_id: null,
    created_by_actor_type: "user",
    created_by_actor_id: null,
    created_at: "2026-05-09T00:00:00Z",
    updated_at: "2026-05-09T00:00:00Z",
    accepted_at: null,
  };
}

function acceptResult() {
  return {
    proposal_id: "proposal-1",
    change_set_id: "change-1",
    context_page_id: "00000000-0000-4000-8000-000000000107",
    context_page_version_id: "version-1",
    tree_node_id: "tree-1",
    feedback_item_id: null,
    review_item_id: null,
    review_required_reason: null,
    audit_event_id: "audit-1",
  };
}

function contextPageNode(id: string, targetId: string, title: string) {
  return {
    id,
    section: "context",
    title,
    slug: null,
    node_kind: "page_ref",
    parent_id: PARENT_ID,
    sort_key: "0002",
    target_kind: "context_page",
    target_id: targetId,
    is_system_node: false,
    can_have_children: true,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [
      {
        id: PARENT_ID,
        section: "context",
        title: "Ask Captures",
        slug: "ask_captures",
        node_kind: "folder",
      },
    ],
  };
}

function treeNode(
  id: string,
  section: string,
  title: string,
  slug: string | null,
): TreeNodePayload {
  return {
    id,
    section,
    title,
    slug,
    node_kind: "folder",
    parent_id: null,
    sort_key: "0001",
    target_kind: null,
    target_id: null,
    is_system_node: true,
    can_have_children: true,
    primary_placement: false,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
  };
}
