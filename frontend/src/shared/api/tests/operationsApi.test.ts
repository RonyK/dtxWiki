import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  archiveMcpChat,
  decideReviewsBulk,
  decideReview,
  getReviewCounts,
  getSettingsPolicies,
  listArtifactProvenance,
  listActivity,
  listAskSessions,
  listLintFindings,
  listMcpChats,
  listReviews,
  readMcpChat,
  searchWorkspace,
  unarchiveMcpChat,
  updateMcpChatTitle,
  updateSettingsPolicyBatch,
  updateSettingsMcpClientStatus,
} from "../operations";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("operations api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("does not downgrade Library policy saves to non-atomic legacy PATCHes", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        { error_code: "not_found", message: "Batch route unavailable", warnings: [] },
        404,
      ),
    );

    await expect(
      updateSettingsPolicyBatch(WORKSPACE_ID, {
        group: "library",
        expected_revision: "a".repeat(64),
        changes: { max_upload_bytes: 20_000_000 },
      }),
    ).rejects.toMatchObject({ status: 404 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies/batch`,
    );
  });

  it("lists activity with filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { items: [], next_cursor: null, unread_count: 0 } }),
    );

    const result = await listActivity(WORKSPACE_ID, {
      period: "week",
      lane: "pending",
      limit: 10,
    });

    expect(result.items).toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=week&lane=pending&limit=10`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("threads cursor through ask session history and returns pagination fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          items: [],
          date_buckets: [],
          next_cursor: "cursor-2",
          total: 73,
          has_more: true,
        },
      }),
    );

    const result = await listAskSessions(WORKSPACE_ID, {
      limit: 50,
      cursor: "cursor-1",
    });

    expect(result.next_cursor).toBe("cursor-2");
    expect(result.total).toBe(73);
    expect(result.has_more).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions?limit=50&cursor=cursor-1`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("threads cursor + severity through lint findings and returns pagination fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          findings: [],
          next_cursor: "lint-2",
          total: 120,
          has_more: true,
        },
      }),
    );

    const result = await listLintFindings(WORKSPACE_ID, {
      status: "open",
      severity: "error",
      cursor: "lint-1",
    });

    expect(result.next_cursor).toBe("lint-2");
    expect(result.has_more).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/lint/findings?status=open&severity=error&cursor=lint-1`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("surfaces search total_count + has_more for the count chip", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          ranked_results: [],
          supported_retrieval_modes: ["keyword"],
          permission_note: null,
          total_count: 42,
          has_more: true,
        },
      }),
    );

    const result = await searchWorkspace(WORKSPACE_ID, { query: "needle" });

    expect(result.total_count).toBe(42);
    expect(result.has_more).toBe(true);
  });

  it("submits review decisions", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: reviewItem("approved") }),
    );

    await decideReview(WORKSPACE_ID, "review-1", "approve", "looks good");

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/review-1/decision`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          decision_type: "approve",
          decision_reason: "looks good",
        }),
      }),
    );
  });

  it("submits bulk review approvals", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { approved: ["review-1"], skipped: [] } }),
    );

    const result = await decideReviewsBulk(WORKSPACE_ID, {
      review_item_ids: ["review-1"],
      filters: null,
      decision_type: "approve",
    });

    expect(result.approved).toEqual(["review-1"]);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decide-bulk`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          review_item_ids: ["review-1"],
          filters: null,
          decision_type: "approve",
        }),
      }),
    );
  });

  it("lists reviews with cursor and risk filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { items: [reviewItem("pending")], next_cursor: "20" } }),
    );

    const result = await listReviews(WORKSPACE_ID, {
      status: "pending",
      risk_level: "high",
      limit: 20,
      cursor: "10",
    });

    expect(result.next_cursor).toBe("20");
    expect(result.items).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&risk_level=high&limit=20&cursor=10`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("fetches review counts with risk filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          pending: 2,
          changes_requested: 1,
          resolved: 3,
          risk_counts: { high: 2, medium: 1, low: 0 },
          to_me: 1,
          from_me: 0,
          auto_approve_candidate_count: 0,
        },
      }),
    );

    const result = await getReviewCounts(WORKSPACE_ID, { risk_level: "high" });

    expect(result.resolved).toBe(3);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/counts?risk_level=high`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("reads policies and posts search payloads", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.endsWith("/settings/policies")) {
        return jsonResponse({ data: policies() });
      }
      if (url.endsWith("/search")) {
        return jsonResponse({
          data: {
            ranked_results: [],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    await getSettingsPolicies(WORKSPACE_ID);
    await searchWorkspace(WORKSPACE_ID, {
      query: "needle",
      scope: "context_page",
      limit: 5,
    });

    expect(fetchMock.mock.calls[1][0].toString()).toBe(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`,
    );
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          query: "needle",
          scope: "context_page",
          filters: {},
          retrieval_mode: "keyword",
          limit: 5,
        }),
      }),
    );
  });

  it("updates MCP client status through settings endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: policies() }),
    );

    await updateSettingsMcpClientStatus(WORKSPACE_ID, "mcp-1", "disabled");

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/mcp-1`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ status: "disabled" }),
      }),
    );
  });

  it("calls MCP chat provenance endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.includes("/mcp-chats/chat-1")) {
        return jsonResponse({ data: mcpChatDetail() });
      }
      if (url.includes("/mcp-chats")) {
        return jsonResponse({
          data: { items: [mcpChatDetail()], next_cursor: null, clients: [] },
        });
      }
      if (url.includes("/mcp-chats/resource-provenance")) {
        return jsonResponse({ data: { items: [] } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    await listMcpChats(WORKSPACE_ID, {
      q: "policy",
      client: "client-1",
      period: "week",
      writes_only: true,
      archived: true,
      sort: "write_count_desc",
      limit: 25,
      cursor: "25",
    });
    await readMcpChat(WORKSPACE_ID, "chat-1");
    await updateMcpChatTitle(WORKSPACE_ID, "chat-1", "Renamed");
    await archiveMcpChat(WORKSPACE_ID, "chat-1");
    await unarchiveMcpChat(WORKSPACE_ID, "chat-1");
    await listArtifactProvenance(WORKSPACE_ID, "context_page", "page-1");

    expect(fetchMock.mock.calls[0][0].toString()).toBe(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=policy&client=client-1&period=week&writes_only=true&archived=true&sort=write_count_desc&limit=25&cursor=25`,
    );
    expect(fetchMock.mock.calls[2]).toEqual([
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ title: "Renamed" }),
      }),
    ]);
    expect(fetchMock.mock.calls[3]).toEqual([
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1`,
      expect.objectContaining({ method: "DELETE" }),
    ]);
    expect(fetchMock.mock.calls[4]).toEqual([
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/chat-1/unarchive`,
      expect.objectContaining({ method: "POST" }),
    ]);
    expect(fetchMock.mock.calls[5][0].toString()).toBe(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats/resource-provenance?kind=context_page&id=page-1`,
    );
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function reviewItem(status: string) {
  return {
    id: "review-1",
    workspace_id: WORKSPACE_ID,
    change_set_id: "change-1",
    review_type: "context_page_publish",
    status,
    risk_level: "medium",
    decision_summary: "Publish requested",
    current_audience_kind: null,
    claimed_by: null,
    claimed_at: null,
    can_decide: true,
    can_claim: false,
    created_at: "2026-05-09T00:00:00Z",
    updated_at: "2026-05-09T00:00:00Z",
    decisions: [],
  };
}

function policies() {
  const card = (name: string) => ({
    name,
    title: name,
    policy: {},
  });
  return {
    workspace: card("workspace"),
    library: card("library"),
    citation: card("citation"),
    context_write: card("context_write"),
    agent_injection: card("agent_injection"),
    mcp_clients: [],
  };
}

function mcpChatDetail() {
  return {
    id: "chat-1",
    workspace_id: WORKSPACE_ID,
    client: {
      id: "client-1",
      name: "Codex CLI",
      client_type: "codex",
      brand_hint: "codex",
      scopes: [],
    },
    mcp_session_id: "session-1",
    title: "Codex CLI · 1 actions · MCP API Page",
    summary: "1 tool calls, 1 writes",
    started_at: "2026-05-25T00:00:00Z",
    last_activity_at: "2026-05-25T00:01:00Z",
    tool_call_count: 1,
    write_count: 1,
    error_count: 0,
    archived_at: null,
    title_user_edited: false,
    title_source: "artifact",
    action: "create",
    subject: "MCP API Page",
    subject_kind: "context_page",
    primary_tool_name: "write_draft",
    linked_artifacts: [],
    timeline: [],
    frozen_summary: null,
    frozen_at: null,
  };
}
