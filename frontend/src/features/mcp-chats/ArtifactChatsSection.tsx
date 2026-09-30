import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  listArtifactProvenance,
  type McpChatArtifactKind,
  type McpChatProvenanceEntry,
} from "../../shared/api/operations";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { StatusPill } from "../../shared/components/StatusPill";
import { safeChatErrorMessage } from "./chatErrors";
import { artifactVerbLabel } from "./chatLabels";
import { chatPresentationLine } from "./chatPresentation";
import { ClientChip } from "./ClientChip";

type ArtifactChatsSectionProps = {
  resourceKind: McpChatArtifactKind;
  resourceId: string;
  variant?: "page" | "inspector";
};

export function ArtifactChatsSection({
  resourceKind,
  resourceId,
  variant = "inspector",
}: ArtifactChatsSectionProps) {
  const { t } = useTranslation("chats");
  const { data, error, loading, reload } = useAsyncLoad<McpChatProvenanceEntry[]>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        return [];
      }
      const provenance = await listArtifactProvenance(
        workspaceId,
        resourceKind,
        resourceId,
      );
      return provenance.items;
    },
    [resourceKind, resourceId],
    {
      errorMessage: t("provenance.loadFailed"),
      formatError: safeChatErrorMessage,
    },
  );
  const items = data ?? [];
  const [expanded, setExpanded] = useState(false);
  // Same component instance is reused as the inspector's selected artifact
  // changes (e.g. clicking between pages); without this, "Show all" from a
  // previous artifact would stay expanded for the next one.
  useEffect(() => {
    setExpanded(false);
  }, [resourceKind, resourceId]);
  const className =
    variant === "page" ? "page-properties-section" : "inspector-section";
  return (
    <section className={className}>
      {variant === "page" ? (
        <h3>{t("provenance.title", { defaultValue: "Chats" })}</h3>
      ) : (
        <header>
          <span>{t("provenance.title", { defaultValue: "Chats" })}</span>
          <span>{items.length}</span>
        </header>
      )}
      {error ? (
        // A failed provenance load must not be presented as the affirmative
        // "not referenced" claim — surface the failure and allow a retry.
        <div className="mcp-provenance-error">
          <p className="page-properties-empty">{error}</p>
          <button
            className="icon-button text-button"
            type="button"
            onClick={reload}
          >
            {t("provenance.retry", { defaultValue: "Retry" })}
          </button>
        </div>
      ) : loading && !data ? (
        <p className="page-properties-empty">
          {t("provenance.loading", { defaultValue: "Loading chats…" })}
        </p>
      ) : items.length === 0 ? (
        <p className="page-properties-empty">
          {t("provenance.empty", {
            defaultValue: "No chat has touched this item yet.",
          })}
        </p>
      ) : (
        <div className="mcp-provenance-list">
          {items.slice(0, expanded ? items.length : 5).map((entry) => (
            <Link
              className="mcp-provenance-link"
              key={`${entry.chat_session.id}-${entry.verb}`}
              to={`/chats/${entry.chat_session.id}`}
            >
              <ClientChip client={entry.chat_session.client} />
              <StatusPill
                compact
                label={artifactVerbLabel(entry.verb, t)}
                status={entry.verb}
                title={artifactVerbLabel(entry.verb, t)}
              />
              <span>
                {chatPresentationLine(entry.chat_session, t)}
                {entry.occurrence_count > 1 ? ` x${entry.occurrence_count}` : ""}
              </span>
              <time
                dateTime={entry.last_occurred_at}
                title={formatVersionTimestamp(entry.last_occurred_at, "long")}
              >
                {formatVersionTimestamp(entry.last_occurred_at)}
              </time>
            </Link>
          ))}
          {items.length > 5 && !expanded ? (
            <button
              className="icon-button text-button"
              onClick={() => setExpanded(true)}
              type="button"
            >
              {t("provenance.showAll", {
                defaultValue: "Show all {{total}}",
                total: items.length,
              })}
            </button>
          ) : null}
        </div>
      )}
    </section>
  );
}
