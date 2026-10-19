import type { TFunction } from "i18next";
import { Check, Copy, FileText } from "lucide-react";
import { createContext, useContext, useEffect, useId, useRef, useState } from "react";
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
  return (
    <span className="mcp-chat-type-chip" title={value}>
      <FileText size={12} aria-hidden="true" />
      {label}
    </span>
  );
}

export function ChatIdentifier({ value, label }: { value: string; label?: string }) {
  const { t } = useTranslation("chats");
  const [open, setOpen] = useState(false);
  const [copying, setCopying] = useState(false);
  const [result, setResult] = useState<CopyTextDetailedResult | null>(null);
  const fullRef = useRef<HTMLElement>(null);
  const mounted = useRef(true);
  const fullId = useId();
  const feedbackId = useId();
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function copy() {
    if (copying) return;
    setCopying(true);
    setResult(null);
    try {
      const next = await copyTextWithReason(value, () => {
        // Reveal only for manual selection, and never after a tab unmount.
        if (!mounted.current) return null;
        flushSync(() => setOpen(true));
        return fullRef.current;
      });
      if (mounted.current) setResult(next);
    } finally {
      if (mounted.current) setCopying(false);
    }
  }

  const reason = result && t(
    `clientIdentity.copyReasons.${result.reason ?? "unsupported"}`,
  );
  const feedback = result && (
    result.status === "copied"
      ? t("detail.identifier.copied")
      : result.status === "selected"
        ? t("clientIdentity.selected", { reason })
        : t("clientIdentity.copyUnavailable", { reason })
  );
  const toggleLabel = label
    ? t(open ? "detail.identifier.hideFor" : "detail.identifier.showFor", { field: label })
    : t(open ? "detail.identifier.hide" : "detail.identifier.show");
  const copyLabel = label
    ? t("detail.identifier.copyFor", { field: label })
    : t("detail.identifier.copy");

  return (
    <div className="mcp-chat-identifier">
      <div className="mcp-chat-identifier-row">
        <button
          type="button"
          className="mcp-chat-id-toggle"
          aria-expanded={open}
          aria-controls={open ? fullId : undefined}
          aria-disabled={copying}
          aria-label={toggleLabel}
          title={toggleLabel}
          onClick={() => {
            if (copying) return;
            setOpen(!open);
            setResult(null);
          }}
        >
          <code dir="ltr">{abbreviatedId(value)}</code>
        </button>
        <button
          type="button"
          className="icon-button compact-icon-button"
          aria-label={copyLabel}
          title={copyLabel}
          aria-describedby={feedback ? feedbackId : undefined}
          aria-disabled={copying}
          onClick={() => void copy()}
        >
          {result?.status === "copied"
            ? <Check size={14} aria-hidden="true" />
            : <Copy size={14} aria-hidden="true" />}
        </button>
      </div>
      {open && <code id={fullId} ref={fullRef} dir="ltr" className="mcp-chat-full-id">{value}</code>}
      <span id={feedbackId} role="status" aria-atomic="true">{feedback}</span>
    </div>
  );
}

export function WorkspaceInput({ value }: { value: string }) {
  const { t } = useTranslation("chats");
  const workspace = useContext(ChatWorkspaces).find((item) => item.id === value);
  const name = workspace && [workspace.title, workspace.name, workspace.slug]
    .find((part) => part?.trim());
  return (
    <div className="mcp-chat-workspace-input">
      <span>{name || t("detail.unknownWorkspace")}</span>
      <ChatIdentifier value={value} label={t("detail.inputFields.workspace_id")} />
    </div>
  );
}
