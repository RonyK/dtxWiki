import { ApiError, apiRequest } from "./client";
import {
  normalizePreviewManifest,
  type PreviewManifest,
} from "../../features/library/preview/model";

export class PreviewManifestEndpointUnavailableError extends Error {
  constructor() {
    super("The Library preview manifest endpoint is unavailable");
    this.name = "PreviewManifestEndpointUnavailableError";
  }
}

export async function getLibraryPreviewManifest(
  workspaceId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<PreviewManifest> {
  let response: unknown;
  try {
    response = await apiRequest<unknown>(
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}` +
        `/library/versions/${encodeURIComponent(versionId)}/preview`,
      { signal },
    );
  } catch (error) {
    if (isPreviewManifestEndpointUnavailable(error)) {
      throw new PreviewManifestEndpointUnavailableError();
    }
    throw error;
  }
  const payload = isRecord(response) && Object.hasOwn(response, "data")
    ? response.data
    : response;
  return normalizePreviewManifest(payload, versionId);
}

export function isPreviewManifestEndpointUnavailable(error: unknown) {
  return (
    error instanceof PreviewManifestEndpointUnavailableError ||
    (error instanceof ApiError && [404, 405, 501].includes(error.status))
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
