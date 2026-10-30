import { ArrowUpRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { workspaceLinkDestinationId } from "../../shared/lib/workspaceLinkBadge";

export type WorkspaceLinkBadgeProps = {
  href: string;
  sourceWorkspaceId?: string | null;
};

/** Noninteractive annotation for a link whose explicit destination is another workspace. */
export function WorkspaceLinkBadge({
  href,
  sourceWorkspaceId,
}: WorkspaceLinkBadgeProps) {
  const { t } = useTranslation("pages");
  const workspaceContext = useOptionalWorkspaceContext();
  const destinationWorkspaceId = workspaceLinkDestinationId(
    href,
    sourceWorkspaceId,
  );
  if (!destinationWorkspaceId) return null;

  const destination = workspaceContext?.workspaces.find(
    (workspace) => workspace.id.toLowerCase() === destinationWorkspaceId,
  );
  const title = destination?.title || destination?.name;
  const label = title ?? t("workspaceLinkBadge.other");
  const accessibleLabel = title
    ? t("workspaceLinkBadge.opensNamed", { title })
    : t("workspaceLinkBadge.opensOther");

  return (
    <span
      className={`workspace-link-badge${title ? " workspace-link-badge--known" : ""}`}
    >
      <ArrowUpRight aria-hidden="true" className="workspace-link-badge__icon" />
      <span aria-hidden="true">{label}</span>
      <span className="visually-hidden">{accessibleLabel}</span>
    </span>
  );
}
