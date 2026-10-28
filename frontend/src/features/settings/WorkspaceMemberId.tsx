import { useTranslation } from "react-i18next";

export function WorkspaceMemberId({
  userId,
  name,
}: {
  userId: string;
  name: string;
}) {
  const { t } = useTranslation("settings");
  return (
    <details className="workspace-member-account-id">
      <summary aria-label={t("workspaceMembers.userIdFor", { name })}>
        <code>{userId.slice(0, 8)}…{userId.slice(-8)}</code>
      </summary>
      <code>{userId}</code>
    </details>
  );
}
