import { createContext, useContext, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";

import type { WorkspaceSummary } from "../../shared/api/workspace";
import {
  copyTextWithReason,
  type CopyTextDetailedResult,
} from "../../shared/lib/copyText";
import { humanizeIdentifier } from "./chatPresentation";

export const ChatWorkspaces = createContext<WorkspaceSummary[]>([]);

export function isIdentifierField(key: string) {
  return key === "id" || key.endsWith("_id");
}

export function isResourceTypeField(key: string) {
  return ["type", "target_type", "target_kind", "resource_kind", "resource_type"].includes(key);
}

export function abbreviatedId(value: string) {
  return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function schemaVersion(value: unknown): string {
  if (value === null || value === undefined || String(value).trim() === "") {
    return "—";
  }
  const text = String(value);
  return /^\d+$/.test(text) ? `v${text}` : text;
}

const RESOURCE_LABELS: Record<string, string> = {
  context_page: "Context Page",
  context_page_version: "Context Page Version",
  library_item: "Library Item",
  library_artifact: "Library Artifact",
  agent_page: "Agent Page",
  context_claim: "Context Claim",
};

export function TypeChip({
  value,
  pageType = false,
}: {
  value: string;
  pageType?: boolean;
}) {
  const { t } = useTranslation("chats");
  const fallback = humanizeIdentifier(value) || "—";
  const label = pageType
    ? t(`detail.pageTypes.${value}`, { defaultValue: fallback })
    : Object.hasOwn(RESOURCE_LABELS, value) ? RESOURCE_LABELS[value] : fallback;
  return <span className="mcp-chat-type-chip" title={value}>{label}</span>;
}

export function ChatIdentifier({ value }: { value: string }) {
  const { t } = useTranslation("chats");
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<CopyTextDetailedResult | null>(null);
  const fullRef = useRef<HTMLElement>(null);

  async function copy() {
    // Commit the full fallback text before the helper tries to select it.
    flushSync(() => setOpen(true));
    setResult(await copyTextWithReason(value, fullRef.current));
  }

  const reason = result && t(
    `clientIdentity.copyReasons.${result.reason ?? "unsupported"}`,
  );
  const feedback = result && (
    result.status === "copied"
      ? t("clientIdentity.copied")
      : result.status === "selected"
        ? t("clientIdentity.selected", { reason })
        : t("clientIdentity.copyUnavailable", { reason })
  );

  return (
    <div className="mcp-chat-identifier">
      <code>{abbreviatedId(value)}</code>
      <div className="mcp-chat-identifier-actions">
        <button
          type="button"
          className="mcp-strip-toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {t(open ? "detail.identifier.hide" : "detail.identifier.show")}
        </button>
        <button type="button" className="mcp-strip-toggle" onClick={copy}>
          {t("detail.identifier.copy")}
        </button>
      </div>
      {open && <code ref={fullRef} className="mcp-chat-full-id">{value}</code>}
      <span role="status">{feedback}</span>
    </div>
  );
}

export function WorkspaceInput({ value }: { value: string }) {
  const { t } = useTranslation("chats");
  const workspace = useContext(ChatWorkspaces).find((item) => item.id === value);
  const name = workspace && [workspace.title, workspace.name, workspace.slug]
    .find((part) => part?.trim());
  return (
    <div>
      {name || t("detail.inputFields.unknownWorkspace")}
      <ChatIdentifier value={value} />
    </div>
  );
}
