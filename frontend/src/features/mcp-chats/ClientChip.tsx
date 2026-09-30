import type { TFunction } from "i18next";
import { Check, ChevronDown, Copy } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { McpChatClientRef } from "../../shared/api/operations";
import {
  copyTextWithReason,
  type CopyTextDetailedResult,
} from "../../shared/lib/copyText";

type ClientChipProps = {
  client: Pick<McpChatClientRef, "name" | "brand_hint">;
};

export function ClientChip({ client }: ClientChipProps) {
  const { t } = useTranslation("chats");
  const brand = brandKey(client);
  const safeName = normalizeClientIdentity(client.name);
  const letter = graphemes(safeName)[0]?.toLocaleUpperCase() || "?";
  const accessibleLabel = boundedClientIdentity(safeName, 48)
    || t("clientIdentity.unknown");
  return (
    <span
      aria-label={accessibleLabel}
      className="mcp-client-chip"
      data-brand={brand}
    >
      {letter}
    </span>
  );
}

type ClientIdentityProps = {
  client: Pick<McpChatClientRef, "id" | "name" | "scopes" | "brand_hint">;
};

/**
 * Full client metadata belongs to the authorized detail route. The collapsed
 * summary stays bounded; expanding it deliberately reveals the sanitized full
 * display name and the granted scopes with localized meanings.
 */
export function ClientIdentity({ client }: ClientIdentityProps) {
  const { t } = useTranslation("chats");
  const fullName = normalizeClientIdentity(client.name)
    || t("clientIdentity.unknown");
  const preview = boundedClientIdentity(fullName, 72);
  const representativeScope = client.scopes[0];
  const scopeSummary = representativeScope
    ? t("clientIdentity.scopeSummary", {
        scope: representativeScope,
        count: client.scopes.length,
      })
    : t("clientIdentity.noPermissions");
  const [expanded, setExpanded] = useState(false);
  const [copyResult, setCopyResult] =
    useState<CopyTextDetailedResult | null>(null);
  const identifierRef = useRef<HTMLElement | null>(null);
  const resetCopyTimerRef = useRef<number | null>(null);
  const copyFeedbackId = useId();
  const copyFeedback = copyResult
    ? clientCopyFeedback(copyResult, t)
    : null;
  const copyLabel = copyFeedback ?? t("clientIdentity.copyIdentifier");

  useEffect(() => () => {
    if (resetCopyTimerRef.current !== null) {
      window.clearTimeout(resetCopyTimerRef.current);
    }
  }, []);

  async function copyIdentifier() {
    const result = await copyTextWithReason(client.id, identifierRef.current);
    setCopyResult(result);
    if (resetCopyTimerRef.current !== null) {
      window.clearTimeout(resetCopyTimerRef.current);
    }
    resetCopyTimerRef.current = window.setTimeout(
      () => setCopyResult(null),
      result.status === "copied" ? 1800 : 5000,
    );
  }

  return (
    <details
      className="mcp-client-identity"
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary
        aria-label={t(
          expanded
            ? "clientIdentity.hideDetailsFor"
            : "clientIdentity.showDetailsFor",
          { name: preview },
        )}
      >
        <span aria-hidden="true">
          <ClientChip client={client} />
        </span>
        <span aria-hidden="true" className="mcp-client-identity-summary-copy">
          <span className="mcp-client-identity-preview" dir="auto">
            {preview}
          </span>
          <span className="mcp-client-identity-scope-summary">
            {scopeSummary}
          </span>
        </span>
        <span aria-hidden="true" className="mcp-client-identity-toggle">
          <ChevronDown aria-hidden="true" size={18} />
        </span>
      </summary>
      <dl className="mcp-client-identity-detail">
        <div>
          <dt>{t("clientIdentity.name")}</dt>
          <dd dir="auto">{fullName}</dd>
        </div>
        <div>
          <dt>{t("clientIdentity.identifier")}</dt>
          <dd className="mcp-client-identifier">
            <code dir="ltr" ref={identifierRef}>
              {client.id}
            </code>
            <button
              aria-describedby={copyFeedback ? copyFeedbackId : undefined}
              aria-label={copyLabel}
              className="icon-button compact-icon-button"
              onClick={() => void copyIdentifier()}
              title={copyLabel}
              type="button"
            >
              {copyResult?.status === "copied"
                ? <Check aria-hidden="true" size={14} />
                : <Copy aria-hidden="true" size={14} />}
            </button>
            {copyFeedback ? (
              <span
                aria-atomic="true"
                className="mcp-client-copy-feedback"
                id={copyFeedbackId}
                role="status"
              >
                {copyFeedback}
              </span>
            ) : null}
          </dd>
        </div>
        <div className="mcp-client-permissions">
          <dt>{t("clientIdentity.permissions")}</dt>
          <dd>
            {client.scopes.length > 0 ? (
              <ul>
                {client.scopes.map((scope) => (
                  <li key={scope}>
                    <code dir="ltr">{scope}</code>
                    <span>{scopeMeaning(scope, t)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <span>{t("clientIdentity.noPermissions")}</span>
            )}
          </dd>
        </div>
      </dl>
    </details>
  );
}

function clientCopyFeedback(
  result: CopyTextDetailedResult,
  t: TFunction<"chats">,
) {
  if (result.status === "copied") {
    return t("clientIdentity.copied");
  }
  const reason = t(
    `clientIdentity.copyReasons.${result.reason ?? "unsupported"}`,
  );
  return result.status === "selected"
    ? t("clientIdentity.selected", { reason })
    : t("clientIdentity.copyUnavailable", { reason });
}

const UNSAFE_IDENTITY_CONTROLS =
  /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/gu;

export function normalizeClientIdentity(value: string): string {
  return value
    .normalize("NFKC")
    .replace(UNSAFE_IDENTITY_CONTROLS, " ")
    .replace(/\s+/gu, " ")
    .trim();
}

export function boundedClientIdentity(value: string, maxChars: number): string {
  const normalized = normalizeClientIdentity(value);
  const characters = graphemes(normalized);
  if (characters.length <= maxChars) {
    return normalized;
  }
  return `${characters.slice(0, Math.max(1, maxChars - 1)).join("")}…`;
}

function graphemes(value: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    return Array.from(segmenter.segment(value), ({ segment }) => segment);
  }
  // Array.from is at least code-point-safe in runtimes that predate Segmenter;
  // current supported browsers take the grapheme-aware branch above.
  return Array.from(value);
}

function scopeMeaning(
  scope: string,
  t: TFunction<"chats">,
): string {
  const key = scope === "mcp:read"
    ? "read"
    : scope === "mcp:write"
      ? "write"
      : scope === "mcp:agent"
        ? "agent"
        : "additional";
  return t(`clientIdentity.scopeMeanings.${key}`);
}

function brandKey(client: Pick<McpChatClientRef, "name" | "brand_hint">) {
  const hint = client.brand_hint?.toLowerCase();
  if (hint && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(hint)) {
    return hint;
  }
  // Display names are neither verified catalog identity nor authorization
  // input. Legacy rows without a server-projected brand remain generic instead
  // of being promoted through fuzzy known-app name matching.
  return "unknown";
}
