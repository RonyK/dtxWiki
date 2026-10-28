import { useTranslation } from "react-i18next";

import {
  type UserDeletionPreview,
  workspaceDeletionSettingsPath,
} from "../api/userDeletion";
import { StatusBanner } from "./StatusBanner";

// Which workspaces a deletion holds with the account, and -- for
// self-withdrawal -- which ones must be dealt with first. Shared by the
// withdrawal panel and the administrator's deletion dialog.
export function UserDeletionImpact({ preview }: { preview: UserDeletionPreview }) {
  const { t } = useTranslation("auth");
  return (
    <>
      {preview.blocking_workspaces.length ? (
        <div className="user-deletion-workspaces">
          <StatusBanner tone="warning">{t("deletion.impact.blocking")}</StatusBanner>
          <ul>
            {preview.blocking_workspaces.map((workspace) => (
              <li key={workspace.workspace_id}>
                <span>
                  {t("deletion.impact.workspaceMembers", {
                    title: workspace.title,
                    count: workspace.other_member_count,
                  })}
                </span>
                <button
                  className="icon-button"
                  onClick={() => {
                    window.open(
                      workspaceDeletionSettingsPath(workspace.workspace_id),
                      "_blank",
                      "noopener,noreferrer",
                    );
                  }}
                  type="button"
                >{t("deletion.impact.openDelete")}</button>
              </li>
            ))}
          </ul>
          <p className="meta-line">{t("deletion.impact.blockingHint")}</p>
        </div>
      ) : null}
      {preview.target_workspaces.length ? (
        <div className="user-deletion-workspaces">
          <p className="meta-line">{t("deletion.impact.held")}</p>
          <ul>
            {preview.target_workspaces.map((workspace) => (
              <li key={workspace.workspace_id}>
                {workspace.other_member_count
                  ? t("deletion.impact.workspaceMembers", {
                      title: workspace.title,
                      count: workspace.other_member_count,
                    })
                  : workspace.title}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}
