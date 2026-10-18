import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  McpChatDetail,
  McpChatTimelineItem,
} from "../../../shared/api/operations";
import { i18n } from "../../../shared/i18n";
import { ChatsDetailScreen } from "../ChatsDetailScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("ChatsDetailScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    // No matchMedia -> useMediaQuery reports false -> the flow section renders
    // its desktop (pinned two-pane) layout, which is what most tests target.
    Reflect.deleteProperty(window, "matchMedia");
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
    Reflect.deleteProperty(window, "matchMedia");
  });

  /** Session info and Artifacts are collapsed by default (§4.2); open one. */
  async function openSection(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
    await user.click(await screen.findByRole("button", { name }));
  }

  /**
   * Each call card carries its own input/result/linked/metadata tabs, always
   * visible. Returns the first call card so a test can scope its queries to
   * the one tabpanel inside it (the flow step itself also has a
   * `role="tabpanel"`, so an unscoped query would match both).
   */
  async function findCallCard() {
    await waitFor(() => {
      expect(document.querySelector(".mcp-chat-flow-call")).not.toBeNull();
    });
    return document.querySelector(".mcp-chat-flow-call") as HTMLElement;
  }

  function callPanel(call: HTMLElement) {
    return within(call).getByRole("tabpanel");
  }

  it.each([false, true])("orders semantic inputs, long content, then identifiers and preserves raw metadata (reverse: %s)", async (reverse) => {
    const user = userEvent.setup();
    const detail = chatDetail();
    const fullId = "12345678-1234-5678-9012-12345678abcd";
    detail.timeline[0] = { ...detail.timeline[0], id: fullId,
      input_summary: { id: fullId, body_markdown: "Raw CASE body ".repeat(30), type: "context_page", workspace_id: WORKSPACE_ID, target_id: "second-id" },
      input_display_items: [{ key: "target_kind", kind: "text", text: "context_page" }],
      headline: "Original RAW headline ".repeat(100), schema_version: "2" };
    if (reverse) {
      detail.timeline[0].input_summary = Object.fromEntries(Object.entries(detail.timeline[0].input_summary).reverse());
    }
    renderWithDetail({ [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]: chatResponse(detail) });
    const card = await findCallCard();
    await user.click(within(card).getByRole("tab", { name: "Inputs" }));
    expect(within(card).getAllByText("Context Page")).toHaveLength(2);
    expect(within(card).getByText("Default")).toBeVisible();
    const technical = card.querySelector(".mcp-tool-technical-inputs")!;
    expect([...technical.querySelectorAll(".mcp-tool-result-meta dt")].map(node => node.textContent))
      .toEqual(reverse ? ["Workspace", "Type", "Target ID", "ID"] : ["Workspace", "Type", "ID", "Target ID"]);
    expect(technical.lastElementChild).toHaveClass("mcp-chat-input-identifiers");
    expect(technical.querySelector(".mcp-tool-longvalue-body")).toHaveTextContent("Raw CASE body");
    await user.click(within(card).getByRole("tab", { name: "Metadata" }));
    const metadata = card.querySelector(".mcp-chat-metadata-grid")!;
    expect(metadata.lastElementChild).toHaveTextContent("12345678…abcd");
    expect(metadata.querySelector(".mcp-chat-call-headline dd")?.textContent).toBe(detail.timeline[0].headline);
    expect(within(card).getByText("v2")).toBeVisible();
  });

  it("renders agent-controlled field names and page types as safe text", async () => {
    const user = userEvent.setup();
    const detail = chatDetail();
    detail.timeline[0].input_summary = Object.fromEntries([
      ["constructor", "first value"],
      ["toString", "second value"],
      ["hasOwnProperty", "third value"],
      ["__proto__", "fourth value"],
      ["page_type", "__proto__"],
    ]);
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]: chatResponse(detail),
    });
    const card = await findCallCard();
    await user.click(within(card).getByRole("tab", { name: "Inputs" }));
    const technical = card.querySelector(".mcp-tool-technical-inputs")!;
    expect([...technical.querySelectorAll("dt")].map(node => node.textContent))
      .toEqual(["Constructor", "ToString", "HasOwnProperty", "Proto", "Page type"]);
    expect(technical.querySelector(".mcp-chat-type-chip")).toHaveTextContent("Proto");
    expect(technical.textContent).not.toContain("returned an object");
  });

  it("renders one title surface, session info, artifacts, and flow metadata", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    expect(await screen.findByRole("heading", { name: "Codex Research Session" }))
      .toBeInTheDocument();
    expect(
      screen.getAllByRole("heading", {
        level: 1,
        name: "Codex Research Session",
      }),
    ).toHaveLength(1);
    expect(screen.queryByText("Search and read activity")).toBeNull();

    // Session info and Artifacts start collapsed; their content is absent
    // until opened, keeping the flow the first thing a reader reaches.
    expect(screen.queryByText("Errors")).not.toBeInTheDocument();
    await openSection(user, /Session info/);
    expect(screen.getByText("Calls").parentElement).toHaveTextContent("3");
    expect(screen.getByText("Errors")).toBeInTheDocument();
    expect(screen.queryByText("3 calls")).not.toBeInTheDocument();

    await openSection(user, /Linked artifacts/);
    expect(screen.getByText("Authentication policy")).toBeInTheDocument();

    // The row is composed from semantic fields, not the stored English
    // headline; it appears in both the flow rail and its pinned panel.
    expect(screen.getAllByText("Search: “policy”").length).toBeGreaterThan(0);
    // The call's tabs are on the card itself; nothing has to be expanded
    // first. A search opens on what it found.
    expect(screen.queryByText("Technical call details")).toBeNull();
    expect(screen.getByRole("tab", { name: "Result" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "Inputs" }));
    // Inputs renders a structured key/value list (not a raw JSON dump).
    expect(screen.getByText("Query")).toBeInTheDocument();
    // The header copy is a full-width line under the title and its actions, not
    // a sentence set inline beside the h1.
    const notice = screen.getByText("Tool activity, not a chat transcript.");
    const staticNote = screen.getByRole("note", {
      name: "Tool activity, not a chat transcript.",
    });
    expect(staticNote).toContainElement(notice);
    expect(staticNote).not.toHaveAttribute("aria-live");
    expect(staticNote).toHaveTextContent(
      "inputs/results retained by its trace policy",
    );
    expect(staticNote).toHaveTextContent(
      "does not receive a separate copy of the message",
    );
    expect(
      screen
        .getByRole("button", { name: "Edit title" })
        .compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders the same non-transcript boundary in Korean", async () => {
    await i18n.changeLanguage("ko");
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    const title = await screen.findByText("채팅 전문이 아닌 도구 활동 기록입니다.");
    const banner = title.closest(".status-banner");
    expect(banner).toHaveTextContent("추적 정책에 따라 보존되는 입력값·결과");
    expect(banner).toHaveTextContent("메시지의 별도 사본을 전송받지는 않습니다");
  });

  it("bounds a max-length client name and reveals full metadata only in detail", async () => {
    const user = userEvent.setup();
    const name = "인증🔐OAuth".repeat(25).slice(0, 200);
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          client: {
            ...chatDetail().client,
            name,
            scopes: ["mcp:read", "mcp:write"],
          },
        }),
    });

    await openSection(user, /Session info/);
    const show = await screen.findByLabelText(/Show full client details for/);
    const metadata = show.closest(".mcp-chat-meta-item");
    expect(metadata).not.toBeNull();
    expect(metadata?.querySelector(`[title="${name}"]`)).toBeNull();
    expect(screen.getByText(name)).not.toBeVisible();

    await user.click(show);

    expect(screen.getByText(name)).toBeVisible();
    expect(screen.getByText("mcp:read")).toBeVisible();
    expect(screen.getByText("mcp:write")).toBeVisible();
  });

  it("shows Restore for an archived chat and unarchives in place (A6)", async () => {
    let unarchiveCalled = false;
    let detailReads = 0;
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]: () => {
        detailReads += 1;
        return chatResponse({
          ...chatDetail(),
          // Archived on first read; the post-restore reload returns it active.
          archived_at: detailReads === 1 ? "2026-06-01T00:00:00Z" : null,
        });
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1/unarchive`]:
        () => {
          unarchiveCalled = true;
          return chatResponse(chatDetail());
        },
    });

    // Archived: Restore is offered, Archive is not.
    const restore = await screen.findByRole("button", { name: "Restore" });
    expect(
      screen.queryByRole("button", { name: "Archive" }),
    ).not.toBeInTheDocument();

    await userEvent.click(restore);

    // The unarchive endpoint is hit and the reloaded (active) chat shows Archive.
    expect(unarchiveCalled).toBe(true);
    const archive = await screen.findByRole("button", { name: "Archive" });
    expect(archive).toBeInTheDocument();
    expect(screen.getByText("Chat restored. Focus remains on its detail actions."))
      .toBeInTheDocument();
    await waitFor(() => expect(archive).toHaveFocus());
  });

  it("keeps the raw headline in Metadata but not on the row", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            { ...chatDetail().timeline[0], headline: "Search: policy" },
          ],
        }),
    });

    expect((await screen.findAllByText("Search: “policy”")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Search: policy")).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: "Metadata" }));
    expect(screen.getByText("Raw headline")).toBeInTheDocument();
    expect(screen.getByText("Search: policy")).toBeInTheDocument();
  });

  it("renders an unknown tool generically without its bare identifier", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "write_draft_v2",
              headline: "Opened the onboarding guide",
              title_source: "generic",
              action: "other",
              subject: null,
              subject_kind: null,
              primary_tool_name: "write_draft_v2",
            },
          ],
        }),
    });

    expect((await screen.findAllByText("MCP activity")).length).toBeGreaterThan(0);
    expect(screen.queryByText("write_draft_v2")).toBeNull();
    expect(screen.queryByText("Opened the onboarding guide")).toBeNull();
  });

  it("labels the metadata strip duration from i18n and formats the span", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    await openSection(user, /Session info/);
    // Label comes from the chats i18n namespace (en: "Duration"); the span is
    // started_at -> last_activity_at (3 minutes) via the shared formatter.
    const duration = (await screen.findByText("Duration")).parentElement;
    expect(duration).toHaveTextContent("3m");
  });

  it("falls back to the i18n unknown label when the duration cannot be computed", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          // last_activity before started -> durationBetween returns null, so the
          // shared formatDurationMs renders the localized unknown label.
          started_at: "2026-05-27T09:03:00Z",
          last_activity_at: "2026-05-27T09:00:00Z",
        }),
    });

    await openSection(user, /Session info/);
    const duration = (await screen.findByText("Duration")).parentElement;
    expect(duration).toHaveTextContent("unknown");
  });

  it("renders the linked-artifacts count as a chip with a pluralized unit", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    expect(await screen.findByText("Linked artifacts")).toBeInTheDocument();
    // One linked artifact in the fixture -> singular unit.
    expect(screen.getByText("1 artifact")).toBeInTheDocument();
  });

  it("renders search matches as muted outputs without treating them as inputs", async () => {
    const user = userEvent.setup();
    const detail = chatDetail();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...detail,
          linked_artifacts: detail.linked_artifacts.map((artifact) => ({
            ...artifact,
            verb: "search_match",
          })),
          timeline: [
            {
              ...detail.timeline[0],
              linked_verbs: ["search_match"],
              linked_artifacts: detail.timeline[0].linked_artifacts.map(
                (artifact) => ({ ...artifact, verb: "search_match" }),
              ),
              input_display_items: [
                { key: "search_query", kind: "text", text: "policy" },
              ],
              result_display_items: [
                {
                  key: "search_match_artifact",
                  kind: "artifact",
                  text: "Authentication policy",
                  resource_kind: "context_page",
                  resource_id: "page-1",
                  navigation_path: "/context?target=page-1",
                },
              ],
            },
          ],
        }),
    });

    await openSection(user, /Linked artifacts/);
    expect(screen.getByText("Search match", { selector: ".status-pill" })).toHaveClass(
      "status-pill-muted",
    );

    // A search opens on its Result tab, where a match is an output...
    const call = await findCallCard();
    expect(within(call).getByRole("tab", { name: "Result" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(callPanel(call)).getByText("Search match")).toBeInTheDocument();
    expect(within(call).queryByText("Result item")).toBeNull();
    // ...and never an input.
    await user.click(within(call).getByRole("tab", { name: "Inputs" }));
    expect(within(callPanel(call)).queryByText("Search match")).toBeNull();
  });

  it("uses the descriptor for the row but keeps a real result summary in the Result tab", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              headline: "Search: policy",
              // A meaningful, non-echo result summary: before this fix it would
              // must remain confined to the Result tab.
              result_summary: "Found 12 matching documents.",
            },
          ],
        }),
    });

    expect((await screen.findAllByText("Search: “policy”")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Search: policy")).toBeNull();

    // The result summary still lives in the Result tab, which a search call
    // opens on.
    const call = await findCallCard();
    expect(
      within(callPanel(call)).getByText("Found 12 matching documents."),
    ).toBeInTheDocument();
  });

  it("surfaces failed tool calls with error code and metadata tab", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              result_status: "failed",
              result_summary: null,
              error_code: "tool_failed",
              latency_ms: 1200,
            },
          ],
        }),
    });

    // Promoted straight into the call, not confined behind a tab. (The status
    // pill also carries "Tool call failed" as its title, so scope to the
    // dedicated failure notice rather than matching by text alone.)
    await waitFor(() => {
      expect(document.querySelector(".mcp-chat-flow-call-failure"))
        .toHaveTextContent("Tool call failed");
    });
    const call = await findCallCard();
    expect(within(call).getByRole("tab", { name: "Result" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(within(call).getByRole("tab", { name: "Metadata" }));
    // Metadata tab now renders a structured key/value list (not a raw JSON dump).
    expect(within(callPanel(call)).getByText("Tool call failed")).toBeInTheDocument();
    expect(within(callPanel(call)).getByText("Call ID")).toBeInTheDocument();
  });

  it("explains a failed write: code label, guidance, no-change badge, and the server message once", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "patch_context_edit",
              action: "edit",
              title_source: "tool",
              subject: null,
              result_status: "failed",
              error_code: "operations_overlap",
              result_summary: "operations 0 and 2 overlap at lines 12-14",
              result_availability: "status_only",
            },
          ],
        }),
    });

    const call = await findCallCard();
    const notice = call.querySelector(".mcp-chat-flow-call-failure") as HTMLElement;
    expect(within(notice).getByText("Overlapping edit operations")).toBeInTheDocument();
    expect(notice).toHaveTextContent(
      "Two operations in the same call target overlapping text.",
    );
    // A write path is one transaction: the failed call changed nothing.
    expect(within(notice).getByText("No changes made")).toBeInTheDocument();
    expect(
      within(notice).getByText("operations 0 and 2 overlap at lines 12-14"),
    ).toBeInTheDocument();
    // The opening Result tab does not print the server message a second time.
    expect(within(call).getByRole("tab", { name: "Result" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(callPanel(call)).not.toHaveTextContent("operations 0 and 2 overlap");
  });

  it("names the overlapping operations for every reader, with lines only when known", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "patch_context_edit",
              action: "edit",
              result_status: "failed",
              error_code: "operations_overlap",
              // Not the caller: no server message, only the structure.
              result_summary: null,
              result_availability: "redacted",
              redaction_reason: "error_restricted",
              error_details: [
                {
                  code: "operations_overlap",
                  conflict: {
                    left_op_index: 0,
                    right_op_index: 2,
                    left_lines: [12, 14],
                    right_lines: [13, 13],
                    overlap_kind: "partial",
                  },
                },
                {
                  // A hidden target keeps the indexes but not the lines.
                  code: "operations_overlap",
                  conflict: {
                    left_op_index: 1,
                    right_op_index: 3,
                    left_lines: null,
                    right_lines: null,
                  },
                },
              ],
            },
          ],
        }),
    });

    const call = await findCallCard();
    const notice = call.querySelector(".mcp-chat-flow-call-failure") as HTMLElement;
    expect(
      within(notice).getByText(
        "Operations 0 and 2 target overlapping text (lines 12–14 and 13).",
      ),
    ).toBeInTheDocument();
    expect(
      within(notice).getByText("Operations 1 and 3 target overlapping text."),
    ).toBeInTheDocument();
    expect(notice.querySelector(".mcp-chat-flow-call-message")).toBeNull();
  });

  it("locates a refused text match by operation, lines and similarity, never by text", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "patch_context_edit",
              action: "edit",
              result_status: "failed",
              error_code: "text_match_ambiguous",
              result_summary: null,
              result_availability: "redacted",
              redaction_reason: "error_restricted",
              error_details: [
                {
                  code: "text_match_ambiguous",
                  field: "operations[1].old_text",
                  operation_index: 1,
                  candidates: [
                    {
                      line_start: 7,
                      line_end: 7,
                      block_id: "b_7",
                      similarity: 0.974,
                      match_kind: "fuzzy",
                    },
                    {
                      line_start: 9,
                      line_end: 10,
                      block_id: "b_9",
                      // Rounds to 100, but the text differed: never shown as 100%.
                      similarity: 0.996,
                      match_kind: "fuzzy",
                    },
                  ],
                },
              ],
            },
          ],
        }),
    });

    const call = await findCallCard();
    const notice = call.querySelector(".mcp-chat-flow-call-failure") as HTMLElement;
    expect(
      within(notice).getByText("Several places matched about equally; nothing applied"),
    ).toBeInTheDocument();
    expect(
      within(notice).getByText(
        "Operation 1 · nearest text (not applied): line 7 · 97% similar",
      ),
    ).toBeInTheDocument();
    expect(
      within(notice).getByText(
        "Operation 1 · nearest text (not applied): line 9–10 · 99% similar",
      ),
    ).toBeInTheDocument();
    expect(within(notice).getByText("No changes made")).toBeInTheDocument();
  });

  it("lists an occurrence-count refusal's matches once per line, not as near misses", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "patch_context_edit",
              action: "edit",
              result_status: "failed",
              error_code: "text_occurrence_mismatch",
              result_summary: null,
              result_availability: "redacted",
              redaction_reason: "error_restricted",
              error_details: [
                {
                  code: "text_occurrence_mismatch",
                  field: "operations[0].old_text",
                  operation_index: 0,
                  // The same word twice on line 3, and once on line 7.
                  candidates: [3, 3, 7].map((line) => ({
                    line_start: line,
                    line_end: line,
                    block_id: null,
                    similarity: 1,
                    match_kind: "exact" as const,
                  })),
                },
              ],
            },
          ],
        }),
    });

    const call = await findCallCard();
    const notice = call.querySelector(".mcp-chat-flow-call-failure") as HTMLElement;
    expect(
      // The server reports only the first few matches, so the row never
      // reads as the complete list.
      within(notice).getByText("Operation 0 · matches include line 3, 7"),
    ).toBeInTheDocument();
    expect(within(notice).queryByText(/nearest text/)).toBeNull();
  });

  it("names an unlabelled error code verbatim and keeps the no-change badge off reads", async () => {
    await i18n.changeLanguage("ko");
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              action: "read",
              result_status: "failed",
              result_summary: null,
              error_code: "media_quota_exceeded",
            },
          ],
        }),
    });

    const call = await findCallCard();
    const notice = call.querySelector(".mcp-chat-flow-call-failure") as HTMLElement;
    // No label in this build, but the code itself is a stable identifier the
    // reader can quote -- better than a generic "도구 호출 오류".
    expect(within(notice).getByText("media_quota_exceeded")).toBeInTheDocument();
    expect(within(notice).queryByText("도구 호출 오류")).toBeNull();
    // A read never had anything to change, so the badge would be noise.
    expect(within(notice).queryByText("변경 없음")).toBeNull();
    expect(notice.querySelector(".mcp-chat-flow-call-message")).toBeNull();
  });

  it("uses safe localized fallbacks for unknown result and error identifiers", async () => {
    await i18n.changeLanguage("ko");
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [{
            ...chatDetail().timeline[0],
            result_status: "future_private_status",
            error_code: "req-secret-code",
          }],
        }),
    });

    expect(await screen.findByText("알 수 없는 상태")).toBeInTheDocument();
    const call = await findCallCard();
    await user.click(within(call).getByRole("tab", { name: "메타데이터" }));
    expect(within(callPanel(call)).getByText("도구 호출 오류")).toBeInTheDocument();
    expect(screen.queryByText(/future_private_status/)).toBeNull();
    expect(screen.queryByText(/req-secret-code/)).toBeNull();
  });

  it("renders linked artifacts as navigable links and shows the schema version", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    const call = await findCallCard();
    await user.click(within(call).getByRole("tab", { name: /Linked/ }));
    const artifactLink = within(callPanel(call)).getByRole("link", {
      name: /Authentication policy/,
    });
    expect(artifactLink).toHaveAttribute("href", "/context?target=page-1");

    await user.click(within(call).getByRole("tab", { name: "Metadata" }));
    expect(within(callPanel(call)).getByText("Schema")).toBeInTheDocument();
    expect(within(callPanel(call)).getByText("v2")).toBeInTheDocument();
  });

  it("shows the status-only note on Result and keeps status and timings in Metadata", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [{
            ...chatDetail().timeline[0],
            result_availability: "status_only",
            result_summary: null,
          }],
        }),
    });

    const call = await findCallCard();
    expect(
      within(callPanel(call)).getByText(
        "Only the result status was retained for this call.",
      ),
    ).toBeInTheDocument();
    // Status sits on the card head and latency beside it, so the always-open
    // Result panel does not repeat them as a grid on every card.
    expect(within(callPanel(call)).queryByText("Latency")).toBeNull();
    expect(within(call).getByText("42 ms")).toBeInTheDocument();

    await user.click(within(call).getByRole("tab", { name: "Metadata" }));
    const metadata = callPanel(call);
    expect(within(metadata).getByText("Status")).toBeInTheDocument();
    expect(within(metadata).getByText("Latency")).toBeInTheDocument();
    expect(within(metadata).getByText("Started")).toBeInTheDocument();
    expect(within(metadata).getByText("Completed")).toBeInTheDocument();
    expect(within(metadata).getByText("42 ms")).toBeInTheDocument();
  });

  it("uses an empty state instead of a blank input JSON panel", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [{
            ...chatDetail().timeline[0],
            input_availability: "not_available",
            input_summary: {},
          }],
        }),
    });

    const call = await findCallCard();
    await user.click(within(call).getByRole("tab", { name: "Inputs" }));
    expect(
      within(callPanel(call)).getByText(
        "No permission-safe input detail is available for this call.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the opening tab, its display items, and the raw inputs without expanding anything", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "update_context_edit",
              action: "edit",
              title_source: "artifact",
              subject: "Deploy procedure",
              subject_kind: "context_page",
              result_availability: "status_only",
              result_summary: null,
              input_summary: { page_id: "page-9", expected_editing_revision: 4 },
              input_display_items: [
                { key: "write_reason", kind: "text", text: "Tighten rollback steps" },
              ],
            },
          ],
        }),
    });

    const call = await findCallCard();
    // No disclosure left anywhere on the card: neither the old outer
    // "Technical call details" nor the nested one inside Inputs.
    expect(call.querySelector("details")).toBeNull();
    // A write opens on what it sent.
    expect(within(call).getByRole("tab", { name: "Inputs" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const panel = callPanel(call);
    expect(within(panel).getByText("Change requested")).toBeInTheDocument();
    expect(within(panel).getByText("Tighten rollback steps")).toBeInTheDocument();
    expect(
      within(panel).getByRole("heading", { name: "Technical inputs" }),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Expected editing revision")).toBeInTheDocument();
    expect(within(panel).getByText("4")).toBeInTheDocument();
    // The old per-call meta line is gone from the card; those facts live in
    // Metadata.
    expect(within(call).queryByText(/^Started /)).toBeNull();
    // The display list renders once, inside the panel, not also above it.
    expect(within(call).getAllByText("Tighten rollback steps")).toHaveLength(1);
  });

  it("opens a write that recorded no input on its Result tab", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "create_context_page",
              action: "create",
              input_availability: "not_applicable",
              input_summary: {},
              input_display_items: [],
              result_display_items: [
                {
                  key: "created_artifact",
                  kind: "artifact",
                  text: "Deploy procedure",
                  resource_kind: "context_page",
                  resource_id: "page-9",
                  navigation_path: "/context?target=page-9",
                },
              ],
            },
          ],
        }),
    });

    const call = await findCallCard();
    expect(within(call).getByRole("tab", { name: "Result" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(callPanel(call)).getByText("Created document")).toBeInTheDocument();
  });

  it("renders the redaction copy on both Result and Inputs", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              input_availability: "redacted",
              input_summary: { workspace_id: WORKSPACE_ID, type: "context_page", id: "private-identifier-1234567890" },
              result_availability: "redacted",
              // A redacted call must not leak what little was captured.
              result_summary: "Found 3 private documents.",
              input_display_items: [
                { key: "search_query", kind: "text", text: "private" },
              ],
            },
          ],
        }),
    });

    // No redaction_reason (a legacy row): the neutral copy, which no longer
    // blames a "referenced item" the reader never saw.
    const call = await findCallCard();
    expect(
      within(callPanel(call)).getByText(
        "The result details of this call are hidden from you.",
      ),
    ).toBeInTheDocument();
    expect(within(call).queryByText("Found 3 private documents.")).toBeNull();

    await user.click(within(call).getByRole("tab", { name: "Inputs" }));
    expect(
      within(callPanel(call)).getByText("The inputs of this call are hidden from you."),
    ).toBeInTheDocument();
    expect(within(callPanel(call)).queryByText("Technical inputs")).toBeNull();
    expect(within(callPanel(call)).queryByText("Default")).toBeNull();
    expect(within(callPanel(call)).queryByText("Context Page")).toBeNull();
    expect(callPanel(call).textContent).not.toContain("private-identifier");
    expect(callPanel(call).querySelector("button")).toBeNull();
  });

  it("applies the returned title and focuses its heading after save", async () => {
    const user = userEvent.setup();
    let title = "Codex Research Session";
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]: () =>
        chatResponse({ ...chatDetail(), title, subject: title }),
    });
    fetchMock.mockImplementation(async (input, init) => {
      const url = input.toString();
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1` &&
        init?.method === "PATCH"
      ) {
        title = JSON.parse(String(init.body)).title;
        return jsonResponse({
          data: { ...chatDetail(), title, subject: title },
        });
      }
      const handler = fetchResponses[url];
      if (!handler) {
        throw new Error(`Unexpected request: ${url}`);
      }
      return jsonResponse(typeof handler === "function" ? handler() : handler);
    });

    renderDetailRoute();

    // Query the visible label rather than the redundant title attribute: the
    // button carries its own text, so the tooltip duplicated it.
    await user.click(
      await screen.findByRole("button", { name: "Edit title" }),
    );
    await user.clear(screen.getByLabelText("Chat title"));
    await user.type(screen.getByLabelText("Chat title"), "Renamed MCP Session");
    await user.click(screen.getByRole("button", { name: "Save title" }));

    const renamedHeading = await screen.findByRole("heading", {
      name: "Renamed MCP Session",
    });
    expect(screen.getByText("Title saved. Focus moved to the updated heading."))
      .toBeInTheDocument();
    await waitFor(() => expect(renamedHeading).toHaveFocus());
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ title: "Renamed MCP Session" }),
      }),
    );
  });

  it("fences a pending title save and restores retry focus after failure", async () => {
    const user = userEvent.setup();
    let rejectPatch!: (reason: Error) => void;
    const patchResult = new Promise<Response>((_resolve, reject) => {
      rejectPatch = reject;
    });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });
    fetchMock.mockImplementation(async (input, init) => {
      const url = input.toString();
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`
        && init?.method === "PATCH"
      ) {
        return patchResult;
      }
      const handler = fetchResponses[url];
      if (!handler) {
        throw new Error(`Unexpected request: ${url}`);
      }
      return jsonResponse(typeof handler === "function" ? await handler() : handler);
    });

    renderDetailRoute();
    await user.click(await screen.findByRole("button", { name: "Edit title" }));
    const input = screen.getByLabelText("Chat title");
    await user.clear(input);
    await user.type(input, "Retryable title");
    await user.click(screen.getByRole("button", { name: "Save title" }));

    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Saving title" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel title edit" })).toBeDisabled();

    rejectPatch(new Error("raw backend request req-secret"));

    expect(await screen.findByText("Failed to update title")).toBeInTheDocument();
    expect(screen.queryByText(/req-secret/)).toBeNull();
    await waitFor(() => expect(input).toBeEnabled());
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveValue("Retryable title");
  });

  it("confirms archive through a dialog before navigating back to Chats", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1#DELETE`]: {
        data: {
          chat_session_id: "chat-1",
          archived_at: "2026-05-27T10:00:00Z",
          audit_event_id: "audit-1",
        },
      },
    });

    renderDetailRoute();

    // The header button now only opens a confirm dialog; nothing is archived yet.
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    const dialog = await screen.findByRole("dialog", {
      name: /Archive Codex Research Session/,
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({ method: "DELETE" }),
    );

    // Confirming inside the dialog fires the DELETE and navigates back.
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(await screen.findByText("Chats landing")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("cancels the archive dialog without calling the endpoint", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
    });

    renderDetailRoute();

    const archiveInvoker = await screen.findByRole("button", { name: "Archive" });
    await user.click(archiveInvoker);
    const dialog = await screen.findByRole("dialog", {
      name: /Archive Codex Research Session/,
    });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(archiveInvoker).toHaveFocus());
    expect(screen.queryByText("Chats landing")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("fences a pending archive and keeps retry focus after a localized failure", async () => {
    const user = userEvent.setup();
    let rejectArchive!: (reason: Error) => void;
    const archiveResult = new Promise<unknown>((_resolve, reject) => {
      rejectArchive = reject;
    });
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse(chatDetail()),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1#DELETE`]:
        () => archiveResult,
    });

    const archive = await screen.findByRole("button", { name: "Archive" });
    await user.click(archive);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));

    expect(within(dialog).getByRole("button", { name: "Archiving" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Close archive dialog" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Close archive dialog" }),
    ).toHaveClass("dialog-close-button");
    expect(within(dialog).getByText(/Other archive actions are paused/))
      .toBeInTheDocument();

    rejectArchive(new Error("raw backend request req-secret"));

    const retry = await within(dialog).findByRole("button", { name: "Archive" });
    expect(within(dialog).getByText("Failed to archive chat")).toBeInTheDocument();
    expect(within(dialog).queryByText(/req-secret/)).toBeNull();
    await waitFor(() => expect(retry).toHaveFocus());
  });

  it("does not display a stored or synthesized English summary", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          // Neither the legacy count nor an English artifact sentence belongs
          // on the localized display surface.
          summary: "3 tool calls, 1 write",
          linked_artifacts: [
            {
              ...chatDetail().linked_artifacts[0],
              verb: "created",
              artifact_title: "Policy Analysis",
            },
          ],
        }),
    });

    expect(
      await screen.findByRole("heading", { name: "Codex Research Session" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("3 tool calls, 1 write")).toBeNull();
    expect(screen.queryByText("Created: Policy Analysis")).toBeNull();
  });

  it("lifts a long tool argument out of the metadata grid and copies it whole", async () => {
    // `body_markdown` is capped near 1 MB server-side. Rendered as a `<dd>` in
    // the auto-fit grid it got a ~120px column and ran for thousands of lines,
    // burying every other argument in the call.
    const user = userEvent.setup();
    const body = `# Heading\n${"policy body line\n".repeat(400)}`;
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              input_summary: { title: "Policy", body_markdown: body },
            },
          ],
        }),
    });

    expect(
      await screen.findByRole("heading", { name: "Codex Research Session" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Inputs" }));
    // The short argument stays in the grid; the long one gets its own block.
    const longValue = await screen.findByRole("heading", {
      name: "Body markdown",
    });
    const block = longValue.closest(".mcp-tool-longvalue");
    expect(block).not.toBeNull();
    expect(block?.querySelector("dl")).toBeNull();
    // Collapsed rendering must truncate the DOM, not just hide overflow.
    expect(block?.textContent?.length ?? 0).toBeLessThan(body.length);

    await user.click(within(block as HTMLElement).getByRole("button", { name: /Copy/ }));

    // Copy always yields the full original, never the truncated preview.
    expect(writeText).toHaveBeenCalledWith(body);
  });

  it("replaces the spinner with a retryable error state when the load fails", async () => {
    const user = userEvent.setup();
    let detailCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`) {
        detailCalls += 1;
        if (detailCalls === 1) {
          throw new Error("raw request req-private-detail");
        }
        return jsonResponse(chatResponse(chatDetail()));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    renderDetailRoute();

    expect(
      await screen.findByText("Failed to load chat detail"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/req-private-detail/)).toBeNull();
    // The failed load no longer pretends to still be in flight.
    expect(
      screen.queryByRole("status", { name: "Loading chat detail" }),
    ).toBeNull();
    expect(screen.getByRole("link", { name: /Back to Chats/ })).toHaveAttribute(
      "href",
      "/chats",
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("heading", { name: "Codex Research Session" }),
    ).toBeInTheDocument();
    expect(detailCalls).toBe(2);
  });

  it("groups the timeline into flow steps, hides bootstrap calls, and switches the panel on selection", async () => {
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          tool_call_count: 3,
          timeline: [
            {
              ...chatDetail().timeline[0],
              id: "tool-index",
              tool_name: "get_index",
              action: "read",
              title_source: "tool",
              subject: null,
              primary_tool_name: "get_index",
            },
            {
              ...chatDetail().timeline[0],
              id: "tool-search",
              tool_name: "search",
              action: "search",
              subject: "policy",
            },
            {
              ...chatDetail().timeline[0],
              id: "tool-read",
              tool_name: "read_page",
              action: "read",
              title_source: "artifact",
              subject: "Authentication policy",
              subject_kind: "context_page",
            },
          ],
        }),
    });

    await screen.findByRole("heading", { name: "Codex Research Session" });

    // The bootstrap `get_index` call is excluded from the flow and summarized
    // inside Session info -- it never becomes a step.
    await openSection(user, /Session info/);
    expect(screen.getByText(/orientation call excluded from the flow above/))
      .toBeInTheDocument();
    expect(screen.getByText(/Get knowledge index/)).toBeInTheDocument();

    // Two steps remain: find (search) and read. Scoped to the rail's own
    // tablist by name, since each call's technical-detail ReaderTabs is also
    // `role="tab"` and would otherwise be counted too.
    const rail = screen.getByRole("tablist", { name: "Work flow" });
    const tabs = within(rail).getAllByRole("tab");
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[1]).toHaveAttribute("aria-selected", "false");
    expect((await screen.findAllByText("Search: “policy”")).length).toBeGreaterThan(0);

    await user.click(tabs[1]);

    expect(tabs[0]).toHaveAttribute("aria-selected", "false");
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect((await screen.findAllByText(/Authentication policy/)).length).toBeGreaterThan(0);
    // The rail keeps listing every step (including "find"), but the pinned
    // panel itself has moved on: the find step's query box is gone from it.
    expect(
      document.querySelector(".mcp-chat-flow-panel--pinned"),
    ).not.toHaveTextContent("Search: “policy”");
  });

  it("falls back to an inline accordion under 1024px instead of a pinned side panel", async () => {
    setViewportWidth(900);
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            { ...chatDetail().timeline[0], id: "tool-search", action: "search", subject: "policy" },
            {
              ...chatDetail().timeline[0],
              id: "tool-read",
              tool_name: "read_page",
              action: "read",
              subject: "Authentication policy",
            },
          ],
        }),
    });

    await screen.findByRole("heading", { name: "Codex Research Session" });

    // Narrow mode drops the rail's tablist/tab semantics in favor of plain
    // disclosures (each call's own technical-detail ReaderTabs keeps its
    // `role="tab"` regardless of width, so this checks the rail specifically).
    expect(screen.queryByRole("tablist", { name: "Work flow" })).toBeNull();
    // "Find"/"Read" each appear twice once a panel is open (once on the rail
    // node, once on the panel's own phase chip), so pick the rail node copy.
    const railNodeFor = (phaseLabel: string) =>
      screen
        .getAllByText(phaseLabel)
        .map((el) => el.closest(".mcp-chat-flow-node"))
        .find((el): el is HTMLElement => el !== null)!;
    const findNode = railNodeFor("Find");
    const readNode = railNodeFor("Read");
    expect(findNode).toHaveAttribute("aria-expanded", "true");
    expect(readNode).toHaveAttribute("aria-expanded", "false");

    // Exactly one inline panel exists at a time (single-open accordion), and
    // it belongs to the selected (first) step -- the rail node titles above
    // are always visible regardless of selection, so this checks the panel
    // specifically rather than presence of the text anywhere on the page.
    await screen.findAllByText("Search: “policy”");
    expect(document.querySelectorAll(".mcp-chat-flow-panel--inline")).toHaveLength(1);
    expect(document.querySelector(".mcp-chat-flow-panel--inline"))
      .not.toHaveTextContent(/Authentication policy/);

    await user.click(readNode);

    expect(readNode).toHaveAttribute("aria-expanded", "true");
    expect(findNode).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => {
      expect(document.querySelectorAll(".mcp-chat-flow-panel--inline")).toHaveLength(1);
      expect(document.querySelector(".mcp-chat-flow-panel--inline"))
        .toHaveTextContent(/Authentication policy/);
    });
  });

  it("shows an explanatory empty state when every call was a bootstrap call", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              tool_name: "get_index",
              action: "read",
              subject: null,
            },
          ],
        }),
    });

    expect(
      await screen.findByText(
        "Every call in this chat was an orientation call (index or listings).",
      ),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });

  it("renders a guide-only chat as a flow step instead of the orientation empty state", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          tool_call_count: 1,
          linked_artifacts: [],
          timeline: [
            {
              ...chatDetail().timeline[0],
              id: "tool-guide",
              tool_name: "guide",
              action: "guide",
              title_source: "tool",
              subject: null,
              subject_kind: null,
              primary_tool_name: "guide",
              linked_verbs: [],
              linked_artifacts: [],
            },
          ],
        }),
    });

    const rail = await screen.findByRole("tablist", { name: "Work flow" });
    const tabs = within(rail).getAllByRole("tab");
    expect(tabs).toHaveLength(1);
    expect(within(tabs[0]).getByText("Other")).toBeInTheDocument();
    expect(within(tabs[0]).getByText("Get workspace guide")).toBeInTheDocument();
    expect(
      screen.queryByText(/Every call in this chat was an orientation call/),
    ).toBeNull();
  });

  it("titles a conflict-then-retry step from the successful retry, not the failed first attempt", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              id: "tool-conflict",
              tool_name: "update_draft",
              action: "edit",
              title_source: "tool",
              subject: null,
              result_status: "conflict",
              result_summary: null,
              error_code: "conflict",
            },
            {
              ...chatDetail().timeline[0],
              id: "tool-retry",
              tool_name: "update_draft",
              action: "edit",
              title_source: "artifact",
              subject: "Deploy procedure v8",
              subject_kind: "context_page",
              result_status: "succeeded",
            },
          ],
        }),
    });

    const rail = await screen.findByRole("tablist", { name: "Work flow" });
    // The rail node reads as the retry's outcome, not the generic tool title
    // ("Update context draft") the failed first attempt alone would produce.
    expect(within(rail).getByText("Edited Deploy procedure v8")).toBeInTheDocument();
    expect(within(rail).queryByText("Update context draft")).toBeNull();
    // The step is still flagged as having had an error.
    expect(within(rail).getByRole("tab")).toHaveClass("is-error");
  });

  it("does not repeat the search query: the backend always echoes it into input_display_items too", async () => {
    // backend/services/mcp_chat_service.py:_input_display_items adds a
    // search_query text item for every search call unconditionally -- if the
    // panel rendered it alongside the query box, the same text would appear
    // twice back to back.
    const user = userEvent.setup();
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              action: "search",
              subject: "rollback criteria",
              input_display_items: [
                { key: "search_query", kind: "text", text: "rollback criteria" },
              ],
              result_display_items: [
                {
                  key: "requested_artifact",
                  kind: "artifact",
                  text: "Rollback criteria and procedure",
                  resource_kind: "context_page",
                  resource_id: "page-2",
                  navigation_path: "/context?target=page-2",
                },
              ],
            },
          ],
        }),
    });

    // The card opens on Result, which lists what the search found.
    await screen.findByText("Rollback criteria and procedure");
    const call = await findCallCard();
    expect(callPanel(call)).not.toHaveTextContent("rollback criteria");
    expect(callPanel(call)).toHaveTextContent("Rollback criteria and procedure");
    // It legitimately still appears once, in the dedicated query box.
    expect(call.querySelector(".mcp-chat-flow-query")).toHaveTextContent(
      "rollback criteria",
    );
    // Inputs drops the echo as well; the raw arguments below it are the
    // fixture's own ("policy"), not the query box's text again.
    await user.click(within(call).getByRole("tab", { name: "Inputs" }));
    expect(within(callPanel(call)).queryByText("Search query")).toBeNull();
    expect(callPanel(call)).not.toHaveTextContent("rollback criteria");
  });

  it("renders a context link in the detail body as the lint queue's bordered, icon-led chip", async () => {
    renderWithDetail({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
        chatResponse({
          ...chatDetail(),
          timeline: [
            {
              ...chatDetail().timeline[0],
              action: "read",
              subject: "Rollback criteria and procedure",
              result_display_items: [
                {
                  key: "read_artifact",
                  kind: "artifact",
                  text: "Rollback criteria and procedure",
                  resource_kind: "context_page",
                  resource_id: "page-2",
                  navigation_path: "/context?target=page-2",
                },
              ],
            },
          ],
        }),
    });

    const link = await screen.findByRole("link", {
      name: "Rollback criteria and procedure",
    });
    // Same class the lint queue's own target chip carries its look through
    // (see 09-lint-findings-fixes.css .lint-target-chip): a bordered,
    // icon-led box, not a bare underlined hyperlink.
    expect(link).toHaveClass("mcp-chat-dl-link");
    expect(link.querySelector("svg")).toBeInTheDocument();
  });

  describe("edit card", () => {
    it("summarizes a patch and previews each operation before and after", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({ ...chatDetail(), timeline: [patchCall()] }),
      });

      const call = await findCallCard();
      const card = within(call).getByRole("region", { name: "What this call changed" });
      expect(card.querySelector(".mcp-chat-edit-summary")).toHaveTextContent(
        "replace_text ×3, insert_after · § Guide › Setup, § FAQ · +120/−45 chars",
      );
      const firstOperation = card.querySelectorAll(".mcp-chat-edit-ops > li")[0] as HTMLElement;
      expect(firstOperation).toHaveTextContent("replace_text");
      expect(firstOperation).toHaveTextContent("§ Guide › Setup");
      expect(firstOperation).toHaveTextContent("L12–14");
      expect(firstOperation).toHaveTextContent("+20/−5 chars");
      // Old text as the "before" line, new text as the "after" line; a
      // truncated preview ends in an ellipsis.
      const before = firstOperation.querySelector(".mcp-chat-edit-line.is-before");
      const after = firstOperation.querySelector(".mcp-chat-edit-line.is-after");
      expect(before).toHaveTextContent("Before: Run the old script");
      expect(after).toHaveTextContent("After: Run the new script…");
      // Previews past the per-call budget are counted, not silently dropped.
      expect(within(card).getByText("2 more not shown")).toBeInTheDocument();
      // A write opens on Inputs; the operations line already heads the card,
      // so the Inputs list does not repeat it.
      expect(within(callPanel(call)).getByText("Deploy procedure")).toBeInTheDocument();
      expect(within(callPanel(call)).queryByText("Operations")).toBeNull();
    });

    it("marks an operation whose text was matched loosely, with how closely", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                operation_counts: { replace_text: 4 },
                operations: [
                  { index: 0, op: "replace_text", match_kind: "fuzzy", match_similarity: 0.982 },
                  { index: 1, op: "replace_text", match_kind: "normalized" },
                  { index: 2, op: "replace_text", match_kind: "exact" },
                  { index: 3, op: "replace_text", match_kind: "fuzzy", match_similarity: 0.9996 },
                ],
                first_change: null,
                preview_budget_exhausted: 0,
              }),
            ],
          }),
      });

      const call = await findCallCard();
      const rows = call.querySelectorAll(".mcp-chat-edit-ops > li");
      const fuzzy = rows[0].querySelector(".mcp-chat-edit-match") as HTMLElement;
      expect(fuzzy).toHaveClass("is-fuzzy");
      expect(fuzzy).toHaveTextContent("Close match 98%");
      expect(fuzzy.getAttribute("title")).toContain("Check the result.");
      expect(rows[1].querySelector(".mcp-chat-edit-match")).toHaveTextContent(
        "Whitespace/Unicode ignored",
      );
      // An exact match is the normal case and carries no badge.
      expect(rows[2].querySelector(".mcp-chat-edit-match")).toBeNull();
      // A text that differed is never "100%" however close it was.
      expect(rows[3].querySelector(".mcp-chat-edit-match")).toHaveTextContent(
        "Close match 99%",
      );
    });

    it("keeps a preview's line breaks under a single marker", async () => {
      const codeBlock = "```sh\n./deploy --dry-run\n./deploy\n```";
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                operation_counts: { insert_after: 1 },
                operations: [{
                  index: 0,
                  op: "insert_after",
                  previews: [{ field: "content", text: codeBlock, truncated: false }],
                }],
                first_change: null,
                preview_budget_exhausted: 0,
              }),
            ],
          }),
      });

      const call = await findCallCard();
      const line = call.querySelector(".mcp-chat-edit-line.is-after") as HTMLElement;
      // The newlines reach the DOM intact (the stylesheet's pre-wrap then
      // lays them out), and the "+" marker is drawn once, not per line.
      expect(line.querySelector(".mcp-chat-edit-text")?.textContent).toBe(codeBlock);
      expect(line.querySelectorAll(".mcp-chat-edit-marker")).toHaveLength(1);
      expect(call.querySelectorAll(".mcp-chat-edit-line")).toHaveLength(1);
    });

    it("lists the first five operations and reveals the rest on request", async () => {
      const user = userEvent.setup();
      const operations = Array.from({ length: 7 }, (_, index) => ({
        index,
        op: "replace_text",
        previews: [],
      }));
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                operation_counts: { replace_text: 7 },
                operations,
                preview_budget_exhausted: 0,
              }),
            ],
          }),
      });

      const call = await findCallCard();
      expect(call.querySelectorAll(".mcp-chat-edit-ops > li")).toHaveLength(5);
      await user.click(within(call).getByRole("button", { name: "Show all 7 operations" }));
      expect(call.querySelectorAll(".mcp-chat-edit-ops > li")).toHaveLength(7);
    });

    it("opens the session's cumulative diff from the existing page diff API", async () => {
      const user = userEvent.setup();
      const diffUrl =
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/page-9/diff`
        + "?from_version_id=ver-1&to_version_id=ver-2";
      const fetchMock = mockFetch({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({ ...chatDetail(), timeline: [patchCall()] }),
        [diffUrl]: { data: pageDiff() },
      });
      renderDetailRoute();

      const call = await findCallCard();
      // Nothing is fetched until the reader asks.
      expect(fetchMock).not.toHaveBeenCalledWith(diffUrl, expect.anything());
      await user.click(within(call).getByRole("button", { name: "View cumulative changes" }));

      const dialog = await screen.findByRole("dialog", {
        name: "Cumulative changes in this edit session",
      });
      expect(await within(dialog).findByText("Run the new script")).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledWith(diffUrl, expect.anything());
    });

    it("previews a whole-body update at its first change and marks input-sized counts as estimates", async () => {
      const user = userEvent.setup();
      const diffUrl =
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/page-9/diff`
        + "?baseline=empty&to_version_id=ver-2";
      const fetchMock = mockFetch({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                source: "input",
                edit_mode: "whole_body",
                operation_counts: {},
                operations: [],
                changed_sections: [],
                chars_added: 300,
                chars_removed: null,
                first_change: {
                  line: 42,
                  before_preview: "Rollback takes an hour.",
                  after_preview: "Rollback takes ten minutes.",
                },
                preview_budget_exhausted: 0,
              }, {
                tool_name: "update_context_edit",
                // A brand-new page: the cumulative diff starts from nothing.
                session_diff: { page_id: "page-9", from_version_id: null, to_version_id: "ver-2" },
              }),
            ],
          }),
        [diffUrl]: { data: pageDiff() },
      });
      renderDetailRoute();

      const call = await findCallCard();
      const card = within(call).getByRole("region", { name: "What this call changed" });
      expect(card.querySelector(".mcp-chat-edit-summary")).toHaveTextContent(
        "+300 chars (estimated)",
      );
      expect(within(card).getByText("First change · line 42")).toBeInTheDocument();
      expect(card.querySelector(".is-before")).toHaveTextContent("Rollback takes an hour.");
      expect(card.querySelector(".is-after")).toHaveTextContent("Rollback takes ten minutes.");

      await user.click(within(card).getByRole("button", { name: "View cumulative changes" }));
      await screen.findByRole("dialog", { name: "Cumulative changes in this edit session" });
      expect(fetchMock).toHaveBeenCalledWith(diffUrl, expect.anything());
    });

    it("shows the start of a created page's body", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                edit_mode: "create",
                operation_counts: {},
                operations: [],
                changed_sections: [],
                chars_added: 2400,
                chars_removed: 0,
                body_preview: { field: "body_md", text: "# Deploy procedure", truncated: true },
                preview_budget_exhausted: 0,
              }, { tool_name: "create_context_page", action: "create", session_diff: null }),
            ],
          }),
      });

      const call = await findCallCard();
      const card = within(call).getByRole("region", { name: "What this call changed" });
      expect(within(card).getByText("Start of the body")).toBeInTheDocument();
      expect(card.querySelector(".is-after")).toHaveTextContent("# Deploy procedure…");
      expect(within(card).queryByRole("button", { name: "View cumulative changes" })).toBeNull();
    });

    it("says why text is missing when previews were withheld or deleted", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              patchCall({
                previews_withheld: true,
                operations: [{ index: 0, op: "replace_text", previews: [] }],
                operation_counts: { replace_text: 1 },
                preview_budget_exhausted: 0,
              }, { id: "tool-withheld", session_diff: null }),
              patchCall({
                previews_purged: true,
                operations: [{
                  index: 0,
                  op: "replace_text",
                  previews: [{ field: "old_text", text: null, truncated: false }],
                }],
                operation_counts: { replace_text: 1 },
                preview_budget_exhausted: 0,
              }, { id: "tool-purged", session_diff: null }),
            ],
          }),
      });

      await findCallCard();
      expect(
        await screen.findByText(
          "The edit's text is shown only to its author. You can see its sizes and operation kinds.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText("The edit previews for this call were deleted."),
      ).toBeInTheDocument();
      // A deleted preview renders no empty before/after line.
      expect(document.querySelector(".mcp-chat-edit-line")).toBeNull();
    });
  });

  describe("redaction reasons", () => {
    it("tells a reader they cannot see the document a failed call worked on, and keeps its shape", async () => {
      const user = userEvent.setup();
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              {
                ...chatDetail().timeline[0],
                tool_name: "patch_context_edit",
                action: "edit",
                title_source: "tool",
                subject: null,
                result_status: "failed",
                error_code: "stale_base_version",
                result_summary: null,
                result_availability: "redacted",
                redaction_reason: "target_hidden",
                input_availability: "available",
                input_summary: { fields: ["operations", "page_id"], expected_editing_revision: 3 },
                input_display_items: [
                  { key: "editing_revision", kind: "text", text: "3" },
                ],
                linked_artifacts: [],
                linked_verbs: [],
              },
            ],
          }),
      });

      const call = await findCallCard();
      expect(call.querySelector(".mcp-chat-flow-call-failure")).toHaveTextContent(
        "Outdated base version",
      );
      expect(
        within(callPanel(call)).getByText(
          "You don't have access to the document this call worked on.",
        ),
      ).toBeInTheDocument();
      // The old copy blamed a "referenced item"; that is gone.
      expect(screen.queryByText(/referenced item/)).toBeNull();

      await user.click(within(call).getByRole("tab", { name: "Inputs" }));
      expect(
        within(callPanel(call)).getByText(
          "You don't have access to the document this call worked on, so only the shape of the request is shown.",
        ),
      ).toBeInTheDocument();
      expect(within(callPanel(call)).getByText("Editing revision")).toBeInTheDocument();
    });

    it("tells everyone but the caller that the server message is the caller's alone", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              {
                ...chatDetail().timeline[0],
                tool_name: "patch_context_edit",
                action: "edit",
                result_status: "failed",
                error_code: "text_not_found",
                result_summary: null,
                result_availability: "redacted",
                redaction_reason: "error_restricted",
              },
            ],
          }),
      });

      const call = await findCallCard();
      expect(
        within(callPanel(call)).getByText(
          "The server's error message is shown only to the person who made this call.",
        ),
      ).toBeInTheDocument();
      expect(call.querySelector(".mcp-chat-flow-call-message")).toBeNull();
    });

    it("prints the caller's server message once, not again as a result item", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              {
                ...chatDetail().timeline[0],
                tool_name: "patch_context_edit",
                action: "edit",
                result_status: "conflict",
                error_code: "stale_base_version",
                result_summary: "server_editing_revision=7",
                result_availability: "available",
                redaction_reason: null,
                result_display_items: [
                  { key: "summary", kind: "text", text: "server_editing_revision=7" },
                ],
              },
            ],
          }),
      });

      const call = await findCallCard();
      expect(within(call).getAllByText("server_editing_revision=7")).toHaveLength(1);
      expect(call.querySelector(".mcp-chat-flow-call-message")).toHaveTextContent(
        "server_editing_revision=7",
      );
    });

    it("localizes a status item instead of printing its machine word", async () => {
      await i18n.changeLanguage("ko");
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({
            ...chatDetail(),
            timeline: [
              {
                ...chatDetail().timeline[0],
                tool_name: "update_context_edit",
                action: "edit",
                input_availability: "not_applicable",
                input_summary: {},
                input_display_items: [],
                result_display_items: [
                  { key: "discarded_edit", kind: "status", text: "discarded" },
                ],
              },
            ],
          }),
      });

      const call = await findCallCard();
      expect(within(callPanel(call)).getByText("폐기된 편집")).toBeInTheDocument();
      expect(within(callPanel(call)).getByText("작성자가 폐기함")).toBeInTheDocument();
      expect(within(call).queryByText("discarded")).toBeNull();
    });
  });

  describe("deleting edit previews", () => {
    it("lets a workspace owner delete a chat's previews after confirming", async () => {
      const user = userEvent.setup();
      let purged = false;
      const fetchMock = mockFetch({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse("owner"),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]: () =>
          chatResponse({
            ...chatDetail(),
            timeline: [
              purged
                ? patchCall({
                    previews_purged: true,
                    operations: [{
                      index: 0,
                      op: "replace_text",
                      previews: [{ field: "old_text", text: null, truncated: false }],
                    }],
                  })
                : patchCall(),
            ],
          }),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1/previews#DELETE`]: () => {
          purged = true;
          return {
            data: {
              chat_session_id: "chat-1",
              purged_call_count: 3,
              audit_event_id: "audit-9",
            },
          };
        },
      });
      renderDetailRoute();

      await user.click(await screen.findByRole("button", { name: "Delete edit previews" }));
      const dialog = await screen.findByRole("dialog", {
        name: "Delete the edit previews in Codex Research Session?",
      });
      // Opening the dialog deletes nothing.
      expect(purged).toBe(false);
      await user.click(within(dialog).getByRole("button", { name: "Delete previews" }));

      expect(
        await screen.findByText("Deleted the edit previews of 3 calls."),
      ).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1/previews`,
        expect.objectContaining({ method: "DELETE" }),
      );
      // The reloaded chat has nothing left to delete, so the action goes.
      await waitFor(() => {
        expect(screen.queryByRole("button", { name: "Delete edit previews" })).toBeNull();
      });
      expect(
        await screen.findByText("The edit previews for this call were deleted."),
      ).toBeInTheDocument();
    });

    it("keeps the action from non-owners and from chats with nothing to delete", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse("editor"),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse({ ...chatDetail(), timeline: [patchCall()] }),
      });
      await findCallCard();
      expect(screen.queryByRole("button", { name: "Delete edit previews" })).toBeNull();
    });

    it("hides the action from an owner when no call holds preview text", async () => {
      renderWithDetail({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse("owner"),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`]:
          chatResponse(chatDetail()),
      });
      await findCallCard();
      expect(screen.queryByRole("button", { name: "Delete edit previews" })).toBeNull();
    });
  });
});

type EditSummary = NonNullable<McpChatTimelineItem["edit_summary"]>;

function patchCall(
  summary: Partial<EditSummary> = {},
  overrides: Partial<McpChatTimelineItem> = {},
): McpChatTimelineItem {
  return {
    ...chatDetail().timeline[0],
    id: "tool-patch",
    tool_name: "patch_context_edit",
    action: "edit",
    title_source: "artifact",
    subject: "Deploy procedure",
    subject_kind: "context_page",
    result_availability: "status_only",
    result_summary: null,
    input_summary: { page_id: "page-9", expected_editing_revision: 4 },
    input_display_items: [
      { key: "context_page", kind: "text", text: "Deploy procedure" },
      { key: "edit_operations", kind: "text", text: "replace_text ×3, insert_after ×1" },
    ],
    edit_summary: {
      source: "receipt",
      edit_mode: "patch",
      operation_counts: { replace_text: 3, insert_after: 1 },
      operations: [
        {
          index: 0,
          op: "replace_text",
          heading_path: ["Guide", "Setup"],
          line_range: [12, 14],
          chars_added: 20,
          chars_removed: 5,
          previews: [
            { field: "old_text", text: "Run the old script", truncated: false },
            { field: "new_text", text: "Run the new script", truncated: true },
          ],
        },
        {
          index: 1,
          op: "insert_after",
          heading_path: ["FAQ"],
          line_range: [40, 40],
          chars_added: 100,
          chars_removed: 0,
          previews: [{ field: "content", text: "A new answer", truncated: false }],
        },
      ],
      changed_sections: [["Guide", "Setup"], ["FAQ"]],
      chars_added: 120,
      chars_removed: 45,
      lines_added: 3,
      lines_removed: 1,
      preview_budget_exhausted: 2,
      previews_purged: false,
      previews_withheld: false,
      ...summary,
    },
    session_diff: { page_id: "page-9", from_version_id: "ver-1", to_version_id: "ver-2" },
    ...overrides,
  };
}

function pageDiff() {
  return {
    baseline: "version",
    from_version_id: "ver-1",
    from_version_no: 1,
    to_version_id: "ver-2",
    to_version_no: 2,
    additions: 1,
    deletions: 1,
    lines: ["--- v1", "+++ v2", "@@ -1,1 +1,1 @@", "-Run the old script", "+Run the new script"],
  };
}

const fetchResponses: Record<
  string,
  unknown | (() => unknown | Promise<unknown>)
> = {};

function renderWithDetail(
  responses: Record<string, unknown | (() => unknown | Promise<unknown>)>,
) {
  mockFetch(responses);
  return renderDetailRoute();
}

function renderDetailRoute() {
  return render(
    <MemoryRouter initialEntries={["/chats/chat-1"]}>
      <Routes>
        <Route path="/chats" element={<div>Chats landing</div>} />
        <Route path="/chats/:chatId" element={<ChatsDetailScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockFetch(
  responses: Record<string, unknown | (() => unknown | Promise<unknown>)>,
) {
  Object.keys(fetchResponses).forEach((key) => {
    delete fetchResponses[key];
  });
  Object.assign(fetchResponses, responses);
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    const key = init?.method === "DELETE" ? `${url}#DELETE` : url;
    const body = fetchResponses[key];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    const resolved = typeof body === "function" ? await body() : body;
    return resolved instanceof Response ? resolved : jsonResponse(resolved);
  });
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === "(max-width: 1024px)" ? width <= 1024 : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function workspaceResponse(role: string | null = null) {
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
          role,
        },
      ],
    },
  };
}

function chatResponse(data: McpChatDetail) {
  return { data };
}

function chatDetail(): McpChatDetail {
  return {
    id: "chat-1",
    workspace_id: WORKSPACE_ID,
    client: {
      id: "client-1",
      name: "Codex CLI",
      client_type: "codex",
      brand_hint: "codex",
      scopes: ["mcp:read"],
    },
    mcp_session_id: "session-1",
    title: "Codex Research Session",
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
    subject: "Codex Research Session",
    subject_kind: null,
    primary_tool_name: "search",
    frozen_summary: null,
    frozen_at: null,
    linked_artifacts: [
      {
        id: "artifact-link-1",
        resource_kind: "context_page",
        resource_id: "page-1",
        artifact_title: "Authentication policy",
        navigation_path: "/context?target=page-1",
        verb: "referenced",
        first_occurred_at: "2026-05-27T09:01:00Z",
        last_occurred_at: "2026-05-27T09:02:00Z",
        occurrence_count: 2,
        last_mcp_tool_call_id: "tool-1",
      },
    ],
    timeline: [
      {
        id: "tool-1",
        tool_name: "search",
        input_summary: { query: "policy", limit: 5, mode: "keyword" },
        input_availability: "available",
        result_status: "succeeded",
        result_summary: "Tool call succeeded.",
        result_availability: "available",
        error_code: null,
        started_at: "2026-05-27T09:01:00Z",
        completed_at: "2026-05-27T09:01:01Z",
        latency_ms: 42,
        headline: "Searched for policy",
        title_source: "query",
        action: "search",
        subject: "policy",
        subject_kind: "query",
        primary_tool_name: "search",
        schema_version: "v2",
        linked_verbs: ["referenced"],
        linked_artifacts: [
          {
            resource_kind: "context_page",
            resource_id: "page-1",
            artifact_title: "Authentication policy",
            navigation_path: "/context?target=page-1",
            verb: "referenced",
          },
        ],
      },
    ],
  };
}
