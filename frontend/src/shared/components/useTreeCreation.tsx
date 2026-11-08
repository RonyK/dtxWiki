import { useEffect, useState } from "react";

import type { TreeNodePayload } from "../api/workspace";
import { NewItemDialog, type NewItemType } from "./NewItemDialog";

export type TreeCreationInput = {
  parentNodeId: string | null;
  title: string;
  slug?: string;
};

export type TreeCreationActions = {
  page?: (input: TreeCreationInput) => Promise<void> | void;
  folder?: (input: TreeCreationInput) => Promise<void> | void;
  upload?: (files: File[], parentNodeId: string | null) => Promise<void> | void;
  uploadAccept?: string;
  typeOrder?: readonly NewItemType[];
};

type CreationRequest = {
  itemType?: NewItemType;
  parentNodeId?: string | null;
};

const DEFAULT_TYPE_ORDER: readonly NewItemType[] = ["page", "upload", "folder"];

/** One creation flow for the rail header and each node's action menu. */
export function useTreeCreation(
  actions: TreeCreationActions | undefined,
  { nodes, rootLabel }: { nodes: readonly TreeNodePayload[]; rootLabel: string },
) {
  const [request, setRequest] = useState<(CreationRequest & { capabilityKey: string }) | null>(null);
  const canCreatePage = Boolean(actions?.page);
  const canCreateFolder = Boolean(actions?.folder);
  const canUpload = Boolean(actions?.upload);
  const capabilityKey = JSON.stringify([canCreatePage, canCreateFolder, canUpload]);
  const typeOrder = actions?.typeOrder ?? DEFAULT_TYPE_ORDER;
  const availableTypes = typeOrder.filter((type) => Boolean(actions?.[type]));
  const enabled = availableTypes.length > 0;
  const activeRequest = request?.capabilityKey === capabilityKey ? request : null;
  const parentNodeId = activeRequest?.parentNodeId ?? null;

  useEffect(() => {
    setRequest(null);
  }, [capabilityKey]);

  function open(nextRequest: CreationRequest = {}) {
    if (enabled) setRequest({ ...nextRequest, capabilityKey });
  }

  const dialog = activeRequest && enabled ? (
    <NewItemDialog
      canCreatePage={canCreatePage}
      canCreateFolder={canCreateFolder}
      canUpload={canUpload}
      uploadAccept={actions?.uploadAccept}
      typeOrder={typeOrder}
      initialItemType={activeRequest.itemType ?? availableTypes[0]}
      initialParentId={parentNodeId ?? ""}
      parentLabel={nodes.find((node) => node.id === parentNodeId)?.title ?? rootLabel}
      parentNodes={nodes.filter((node) => node.can_have_children)}
      rootLabel={rootLabel}
      onClose={() => setRequest((current) => current === activeRequest ? null : current)}
      onSubmit={({ itemType, ...input }) => {
        const handler = itemType === "page" ? actions?.page : actions?.folder;
        return handler?.(input);
      }}
      onPickFiles={actions?.upload}
    />
  ) : null;

  return { open, canCreatePage, canCreateFolder, canUpload, enabled, dialog };
}
