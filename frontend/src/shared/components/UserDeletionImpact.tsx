import { useTranslation } from "react-i18next";

import type { UserDeletionPreview } from "../api/userDeletion";
import { StatusBanner } from "./StatusBanner";

// What a deletion request removes and hands over, shared by self-withdrawal
// and the administrator's deletion dialog.
export function UserDeletionImpact({ preview }: { preview: UserDeletionPreview }) {
  const { t } = useTranslation("auth");
  const personal = preview.personal_workspace;
  return (
    <>
      {personal && personal.other_member_count > 0 ? (
        <StatusBanner tone="warning">
          {t("deletion.impact.personal", { title: personal.title })}{" "}
          {t("deletion.impact.personalShared", { count: personal.other_member_count })}
        </StatusBanner>
      ) : personal ? (
        <p className="meta-line">{t("deletion.impact.personal", { title: personal.title })}</p>
      ) : null}
      {preview.transferred_workspaces.length ? (
        <div className="user-deletion-transfers">
          <p className="meta-line">{t("deletion.impact.transferred")}</p>
          <ul>
            {preview.transferred_workspaces.map((workspace) => (
              <li key={workspace.workspace_id}>{workspace.title}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}
