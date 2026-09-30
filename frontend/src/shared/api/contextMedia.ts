import { apiRequest } from "./client";
import type { components } from "./types.gen";
import {
  isContextImageMediaType,
  type ContextImageMediaType,
  uploadPolicyForMediaTypes,
} from "../media/formatCapabilities";

const CONTEXT_MEDIA_ASSET_REFERENCE =
  /^dtx-media:\/\/asset\/([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$/;
const CONTEXT_MEDIA_ASSET_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type GeneratedContextMediaCapabilities =
  components["schemas"]["ContextMediaCapabilitiesPayload"];

export type ContextMediaCapabilities = Omit<
  GeneratedContextMediaCapabilities,
  | "uploadable_media_types"
  | "max_files_per_batch"
  | "max_batch_bytes"
  | "max_parallel_uploads"
> & {
  uploadable_media_types: ContextImageMediaType[];
  max_files_per_batch?: number;
  max_batch_bytes?: number;
  max_parallel_uploads?: number;
};

export const PENDING_CONTEXT_MEDIA_CAPABILITIES: ContextMediaCapabilities = {
  policy_revision: "",
  uploadable_media_types: [],
  max_upload_bytes: 0,
  max_files_per_batch: 0,
  max_batch_bytes: 0,
  max_parallel_uploads: 0,
};

export type ContextMediaUploadFields = {
  asset_id?: string;
  logical_ref?: string;
  content_url?: string;
};

export type ContextMediaAssetUnavailableReason =
  | "resolver_unavailable"
  | "version_unavailable"
  | "asset_url_unavailable";

export type ContextMediaAssetUrlResolution =
  | { status: "ready"; url: string }
  | {
      status: "unavailable";
      reason: ContextMediaAssetUnavailableReason;
    };

export type ContextMediaAssetUrlResolver = (
  assetId: string,
) => ContextMediaAssetUrlResolution;

export type ContextMediaUsageFilter = "used" | "unused";
export type ContextMediaUsageState = "active" | "history_only" | "unused";

export type ContextMediaAsset = {
  id: string;
  logical_ref: string;
  original_filename: string;
  media_type: ContextImageMediaType;
  bytes: number;
  association_state: "pending_install" | "active" | "tombstoned";
  usage_state: ContextMediaUsageState;
  latest_draft_reference_count: number;
  current_published_reference_count: number;
  historical_reference_count: number;
  can_delete: boolean;
  usage_token: string;
  content_url: string;
};

export type ContextMediaDeleteResult = {
  id: string;
  association_state: "tombstoned";
  gc_state: "queued";
};

type ContextMediaAssetListResponse = {
  data: ContextMediaAsset[];
};

type ContextMediaDeleteResponse = {
  data: ContextMediaDeleteResult;
};

export async function getContextMediaCapabilities(workspaceId: string) {
  const response = await apiRequest<{ data: unknown }>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/media/capabilities`,
  );
  return normalizeContextMediaCapabilities(response.data);
}

export function contextMediaUploadPolicy(
  capabilities: ContextMediaCapabilities,
) {
  return uploadPolicyForMediaTypes(
    capabilities.uploadable_media_types,
    capabilities.max_upload_bytes,
  );
}

export function contextMediaCapabilitiesAreCurrent(
  capabilities: ContextMediaCapabilities,
) {
  return Boolean(
    capabilities.policy_revision.trim() &&
    capabilities.uploadable_media_types.length > 0 &&
    capabilities.max_upload_bytes > 0 &&
    (capabilities.max_files_per_batch ?? 0) > 0 &&
    (capabilities.max_batch_bytes ?? 0) > 0 &&
    (capabilities.max_parallel_uploads ?? 0) > 0,
  );
}

export async function listPageContextMedia(
  workspaceId: string,
  pageId: string,
  usage: ContextMediaUsageFilter,
) {
  const response = await apiRequest<ContextMediaAssetListResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}/media?usage=${usage}`,
  );
  return response.data;
}

export async function deletePageContextMedia(
  workspaceId: string,
  assetId: string,
  usageToken: string,
) {
  const response = await apiRequest<ContextMediaDeleteResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/media/assets/${encodedAssetId(assetId)}`,
    {
      method: "DELETE",
      headers: { "If-Match": usageToken },
    },
  );
  return response.data;
}

export function contextMediaAssetIdFromReference(reference: string) {
  return CONTEXT_MEDIA_ASSET_REFERENCE.exec(reference)?.[1] ?? null;
}

export function contextMediaPageVersionContentUrl(
  workspaceId: string,
  pageId: string,
  versionId: string,
  assetId: string,
) {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}/versions/${encodeURIComponent(
    versionId,
  )}/media/${encodedAssetId(assetId)}/content`;
}

export function contextMediaPublicContentUrl(
  workspaceSlug: string,
  pageSlug: string,
  assetId: string,
) {
  return `/api/v1/public/w/${encodeURIComponent(
    workspaceSlug,
  )}/pages/${encodeURIComponent(pageSlug)}/media/${encodedAssetId(assetId)}`;
}

function encodedAssetId(assetId: string) {
  if (!CONTEXT_MEDIA_ASSET_ID.test(assetId)) {
    throw new Error("Context media asset id was invalid.");
  }
  return encodeURIComponent(assetId);
}

function normalizeContextMediaCapabilities(
  value: unknown,
): ContextMediaCapabilities {
  if (!value || typeof value !== "object") {
    return copyPendingCapabilities();
  }
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.policy_revision !== "string" ||
    !candidate.policy_revision.trim() ||
    !Array.isArray(candidate.uploadable_media_types) ||
    typeof candidate.max_upload_bytes !== "number" ||
    !Number.isFinite(candidate.max_upload_bytes) ||
    candidate.max_upload_bytes <= 0
  ) {
    return copyPendingCapabilities();
  }
  const mediaTypes: ContextImageMediaType[] = [];
  for (const entry of candidate.uploadable_media_types) {
    if (typeof entry !== "string") {
      return copyPendingCapabilities();
    }
    const normalized = entry.trim().toLowerCase();
    if (!isContextImageMediaType(normalized)) {
      return copyPendingCapabilities();
    }
    if (!mediaTypes.includes(normalized)) {
      mediaTypes.push(normalized);
    }
  }
  if (mediaTypes.length === 0) {
    return copyPendingCapabilities();
  }
  return {
    policy_revision: candidate.policy_revision.trim(),
    uploadable_media_types: mediaTypes,
    max_upload_bytes: Math.trunc(candidate.max_upload_bytes),
    max_files_per_batch: normalizedOptionalPositiveInteger(
      candidate.max_files_per_batch,
      1,
    ),
    max_batch_bytes: normalizedOptionalPositiveInteger(
      candidate.max_batch_bytes,
      10 * 1024 * 1024,
    ),
    max_parallel_uploads: normalizedOptionalPositiveInteger(
      candidate.max_parallel_uploads,
      1,
    ),
  };
}

function normalizedOptionalPositiveInteger(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) && value >= 1
    ? Math.trunc(value)
    : fallback;
}

function copyPendingCapabilities(): ContextMediaCapabilities {
  return {
    ...PENDING_CONTEXT_MEDIA_CAPABILITIES,
    uploadable_media_types: [],
  };
}
