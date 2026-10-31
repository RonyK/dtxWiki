import { Pin, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphExpansionStep } from "./workspaceGraphExpansion";
import { formatCount } from "./workspaceGraphLabels";

/** Groups opened by expansion, in replay order; pinned ones fold last. */
export function WorkspaceGraphExpandedGroups({ busy, onCollapse, onTogglePin, steps }: {
  busy: boolean;
  onCollapse: (clusterId: string) => void;
  onTogglePin: (clusterId: string) => void;
  steps: ReadonlyArray<Omit<WorkspaceGraphExpansionStep, "offset">>;
}) {
  const { t } = useTranslation("graph");
  return (
    // Focus lands here after a replay when the relationship list is hidden.
    <section aria-label={t("workspaceGraph.expandedGroups.label")} className="workspace-graph-expanded-groups" tabIndex={-1}>
      <span className="workspace-graph-expanded-groups-title" title={t("workspaceGraph.expandedGroups.pinnedHint")}>
        {t("workspaceGraph.expandedGroups.label")}
      </span>
      <ul>
        {steps.map((step) => (
          <li data-folded={step.visible ? undefined : "true"} data-pinned={step.pinned ? "true" : "false"} key={step.clusterId}
            title={step.visible ? undefined : t("workspaceGraph.expandedGroups.folded")}>
            <button aria-label={t("workspaceGraph.expandedGroups.pin", { label: step.label })} aria-pressed={step.pinned}
              className="workspace-graph-expanded-pin" onClick={() => onTogglePin(step.clusterId)}
              title={t("workspaceGraph.expandedGroups.pinnedHint")} type="button">
              <Pin aria-hidden="true" size={14} />
            </button>
            <span title={step.label}>{step.label}</span>
            <small>{t("workspaceGraph.expandedGroups.count", { visible: formatCount(step.visible), total: formatCount(step.total) })}</small>
            <button aria-label={t("workspaceGraph.expandedGroups.collapse", { label: step.label })}
              className="workspace-graph-expanded-collapse" disabled={busy} onClick={() => onCollapse(step.clusterId)}
              title={t("workspaceGraph.expandedGroups.collapse", { label: step.label })} type="button">
              <X aria-hidden="true" size={14} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
