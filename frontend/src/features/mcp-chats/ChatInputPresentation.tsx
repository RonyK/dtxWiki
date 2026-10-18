import type { TFunction } from "i18next";
import { createContext, useContext, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";

import type { WorkspaceSummary } from "../../shared/api/workspace";
import {
  copyTextWithReason,
  type CopyTextDetailedResult,
} from "../../shared/lib/copyText";
import enChats from "../../shared/i18n/locales/en/chats.json";
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
  library_item_version: "Library Item Version",
  library_artifact: "Library Artifact",
  agent_page: "Agent Page",
  context_claim: "Context Claim",
};

// Agent-supplied keys must not reach i18next's inherited-property lookup.
// The canonical dictionary also rejects dotted paths and object-valued keys.
export function chatInputLabel(key: string, t: TFunction<"chats">): string {
  return Object.hasOwn(enChats.detail.inputFields, key)
    ? t(`detail.inputFields.${key}`)
    : humanizeIdentifier(key) || "—";
}

export function TypeChip({
  value,
  pageType = false,
}: {
  value: string;
  pageType?: boolean;
}) {
  const { t } = useTranslation("chats");
  const fallback = humanizeIdentifier(value).trim() || "—";
  const label = pageType
    ? Object.hasOwn(enChats.detail.pageTypes, value)
      ? t(`detail.pageTypes.${value}`)
      : fallback
    : Object.hasOwn(RESOURCE_LABELS, value) ? RESOURCE_LABELS[value] : fallback;
  return <span className="mcp-chat-type-chip" title={value}>{label}</span>;
}

export function ChatIdentifier({ value, label }: { value: string; label?: string }) {
  const { t } = useTranslation("chats");
  const [open, setOpen] = useState(false);
  const [copying, setCopying] = useState(false);
  const [result, setResult] = useState<CopyTextDetailedResult | null>(null);
  const fullRef = useRef<HTMLElement>(null);

  async function copy() {
    if (copying) return;
    // Keep the full fallback mounted while clipboard permission is pending.
    // A hide or duplicate copy must not detach the helper's selection target.
    flushSync(() => {
      setOpen(true);
      setCopying(true);
      setResult(null);
    });
    try {
      setResult(await copyTextWithReason(value, fullRef.current));
    } finally {
      setCopying(false);
    }
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
          disabled={copying}
          aria-label={label ? t(open ? "detail.identifier.hideFor" : "detail.identifier.showFor", { field: label }) : undefined}
          onClick={() => {
            setOpen(!open);
            setResult(null);
          }}
        >
          {t(open ? "detail.identifier.hide" : "detail.identifier.show")}
        </button>
        <button
          type="button"
          className="mcp-strip-toggle"
          aria-label={label ? t("detail.identifier.copyFor", { field: label }) : undefined}
          disabled={copying}
          onClick={copy}
        >
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
      <ChatIdentifier value={value} label={t("detail.inputFields.workspace_id")} />
    </div>
  );
}
