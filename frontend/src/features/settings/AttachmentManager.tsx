import { useState } from "react";
import { useTranslation } from "react-i18next";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import {
  AttachmentsSection,
  type AttachmentsScope,
} from "./AttachmentsSection";

export function AttachmentManager({
  workspaceId,
  isServerAdmin,
  canViewEveryUpload,
}: {
  workspaceId: string | null;
  isServerAdmin: boolean;
  canViewEveryUpload: boolean;
}) {
  const { t } = useTranslation("settings");
  const [scope, setScope] = useState<AttachmentsScope>("workspace");
  const effectiveScope = isServerAdmin ? scope : "workspace";
  return (
    <>
      {isServerAdmin ? (
        <fieldset className="attachments-scope">
          <SegmentedControl
            label={t("attachments.scope.label")}
            value={effectiveScope}
            options={[
              { key: "workspace", label: t("attachments.scope.workspace") },
              { key: "server", label: t("attachments.scope.server") },
            ]}
            onChange={setScope}
          />
        </fieldset>
      ) : null}
      <AttachmentsSection
        key={`${workspaceId}:${effectiveScope}`}
        workspaceId={workspaceId}
        scope={effectiveScope}
        canViewEveryUpload={canViewEveryUpload}
      />
    </>
  );
}
