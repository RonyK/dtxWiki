import { afterEach, describe, expect, it } from "vitest";

import type { McpChatTimelineItem } from "../../../shared/api/operations";
import { i18n } from "../../../shared/i18n";
import {
  chatPresentationLine,
  humanizeIdentifier,
} from "../chatPresentation";

function presentation(
  values: Partial<McpChatTimelineItem>,
): McpChatTimelineItem {
  return values as McpChatTimelineItem;
}

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("chatPresentationLine", () => {
  it("keeps a user-authored title verbatim in every locale", async () => {
    const row = presentation({
      title_source: "user",
      action: "edit",
      subject: "Q3 OAuth 검토",
      subject_kind: null,
      primary_tool_name: "update_draft",
    });

    await i18n.changeLanguage("en");
    expect(chatPresentationLine(row, i18n.getFixedT("en", "chats"))).toBe(
      "Q3 OAuth 검토",
    );
    await i18n.changeLanguage("ko");
    expect(chatPresentationLine(row, i18n.getFixedT("ko", "chats"))).toBe(
      "Q3 OAuth 검토",
    );
  });

  it("localizes artifact labels but preserves the document title verbatim", async () => {
    const row = presentation({
      title_source: "artifact",
      action: "edit",
      subject: "인증 API Policy",
      subject_kind: "context_page",
      primary_tool_name: "update_draft",
    });

    await i18n.changeLanguage("en");
    expect(chatPresentationLine(row, i18n.getFixedT("en", "chats"))).toBe(
      "Edited 인증 API Policy",
    );

    await i18n.changeLanguage("ko");
    expect(chatPresentationLine(row, i18n.getFixedT("ko", "chats"))).toBe(
      "인증 API Policy 편집",
    );
  });

  it("localizes search labels while preserving the raw query", async () => {
    const row = presentation({
      title_source: "query",
      action: "search",
      subject: "OAuth refresh 정책",
      subject_kind: "query",
      primary_tool_name: "search",
    });

    await i18n.changeLanguage("ko");
    expect(chatPresentationLine(row, i18n.getFixedT("ko", "chats"))).toBe(
      "검색: “OAuth refresh 정책”",
    );
  });

  it("does not expose an unknown tool identifier in the generic fallback", async () => {
    const row = presentation({
      title_source: "generic",
      action: "other",
      subject: null,
      subject_kind: null,
      primary_tool_name: "write_draft_v2",
    });

    await i18n.changeLanguage("ko");
    const visible = chatPresentationLine(
      row,
      i18n.getFixedT("ko", "chats"),
    );
    expect(visible).toBe("MCP 활동");
    expect(visible).not.toContain("write_draft_v2");
  });

  it("uses the localized tool title when a known call has no subject", async () => {
    const row = presentation({
      title_source: "tool",
      action: "guide",
      subject: null,
      subject_kind: null,
      primary_tool_name: "guide",
    });

    await i18n.changeLanguage("ko");
    expect(chatPresentationLine(row, i18n.getFixedT("ko", "chats"))).toBe(
      "워크스페이스 가이드 보기",
    );
  });

  it("localizes created and focused-read artifact verbs without changing the title", async () => {
    const created = presentation({
      title_source: "artifact",
      action: "create",
      subject: "MCP 설정",
      subject_kind: "context_page",
      primary_tool_name: "write_draft",
    });
    const read = presentation({
      title_source: "artifact",
      action: "read",
      subject: "아키텍처 가이드",
      subject_kind: "context_page",
      primary_tool_name: "read_concept",
    });

    expect(chatPresentationLine(created, i18n.getFixedT("en", "chats"))).toBe(
      "Created MCP 설정",
    );
    expect(chatPresentationLine(read, i18n.getFixedT("en", "chats"))).toBe(
      "Read 아키텍처 가이드",
    );
    expect(chatPresentationLine(created, i18n.getFixedT("ko", "chats"))).toBe(
      "MCP 설정 작성",
    );
    expect(chatPresentationLine(read, i18n.getFixedT("ko", "chats"))).toBe(
      "아키텍처 가이드 읽기",
    );
  });

  it("uses action-only localized names for bootstrap-only sessions", () => {
    const workspaces = presentation({
      title_source: "tool",
      action: "read",
      subject: null,
      subject_kind: null,
      primary_tool_name: "list_my_workspaces",
    });

    expect(chatPresentationLine(workspaces, i18n.getFixedT("en", "chats"))).toBe(
      "List my workspaces",
    );
    expect(chatPresentationLine(workspaces, i18n.getFixedT("ko", "chats"))).toBe(
      "내 워크스페이스 목록",
    );
  });

  it("does not humanize an unknown subject kind into English or expose its identifier", () => {
    const row = presentation({
      title_source: "target_kind",
      action: "other",
      subject: null,
      subject_kind: "future_private_kind",
      primary_tool_name: "future_tool",
    });

    const visible = chatPresentationLine(row, i18n.getFixedT("ko", "chats"));
    expect(visible).toBe("워크스페이스 항목 · 기타");
    expect(visible).not.toContain("future_private_kind");
    expect(visible).not.toContain("Future private kind");
  });
});

describe("humanizeIdentifier", () => {
  it("removes machine separators", () => {
    expect(humanizeIdentifier("write_draft-v2")).toBe("Write draft v2");
  });
});
