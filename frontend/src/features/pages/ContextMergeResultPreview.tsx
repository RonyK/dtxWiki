import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ContextMergeCandidate } from "../../shared/api/pages";
import { readTreeNode } from "../../shared/api/workspace";
import { MarkdownView } from "./MarkdownView";

/** The exact body and user metadata whose hash the approval command binds. */
export function ContextMergeResultPreview({ candidate, workspaceId }: {
  candidate: ContextMergeCandidate;
  workspaceId?: string | null;
}) {
  const { t } = useTranslation("pages");
  const metadata = candidate.page_metadata;
  const destinationId = typeof metadata.placement_parent_node_id === "string" ? metadata.placement_parent_node_id : null;
  const [destination, setDestination] = useState<{ id: string; label: string } | null>(null);
  useEffect(() => {
    let active = true;
    if (workspaceId && destinationId) {
      void readTreeNode(workspaceId, destinationId).then((node) => {
        if (active) setDestination({ id: destinationId, label: [...node.breadcrumb.filter((entry) => entry.id !== node.id).map((entry) => entry.title), node.title].join(" / ") });
      }).catch(() => undefined);
    }
    return () => { active = false; };
  }, [workspaceId, destinationId]);
  const listValue = (value: unknown) => Array.isArray(value) ? value.filter((item) => typeof item === "string").join(", ") : "";
  return (
    <section className="context-merge-result-preview">
      {typeof metadata.title === "string" ? <h3>{metadata.title}</h3> : null}
      <dl className="detail-list">
        {typeof metadata.page_type === "string" ? <div><dt>{t("editor.pageType")}</dt><dd>{t(`editor.pageTypes.${metadata.page_type}`, { defaultValue: metadata.page_type })}</dd></div> : null}
        <div><dt>{t("editor.tagsAria")}</dt><dd>{listValue(metadata.tags) || "—"}</dd></div>
        <div><dt>{t("editor.aliasesLabel")}</dt><dd>{listValue(metadata.aliases) || "—"}</dd></div>
        <div><dt>{t("editor.destinationLabel")}</dt><dd>{destinationId
          ? destination?.id === destinationId ? destination.label : t("editor.mergeDestinationUnavailable")
          : t("editor.destinationEmptyLabel")}</dd></div>
      </dl>
      <MarkdownView body={candidate.body_md} workspaceId={workspaceId} />
    </section>
  );
}
