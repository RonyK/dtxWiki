import {
  ApiError,
  type ApiErrorBody,
  getApiBaseUrl,
  getApiToken,
} from "./client";
import type { components } from "./types.gen";

export type ImageUploadResult =
  components["schemas"]["ContextMediaUploadPayload"];

export class ImageUploadApiError extends ApiError {
  readonly retryAfterMs: number | null;

  constructor(
    status: number,
    body: ApiErrorBody,
    retryAfterMs: number | null,
  ) {
    super(status, body);
    this.name = "ImageUploadApiError";
    this.retryAfterMs = retryAfterMs;
  }
}

export type ImageUploadInput = {
  file: File;
  marker: string;
  editingVersionId: string;
  workspaceId: string;
  pageId: string;
  policyRevision?: string;
  signal?: AbortSignal;
};

type ImageUploadResponse = {
  data?: Partial<ImageUploadResult>;
};

export async function uploadImage({
  file,
  marker,
  editingVersionId,
  workspaceId,
  pageId,
  policyRevision,
  signal,
}: ImageUploadInput): Promise<ImageUploadResult> {
  const endpoint = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}/media`;
  const formData = new FormData();
  formData.set("file", file);
  formData.set("marker_uuid", marker);
  formData.set("editing_version_id", editingVersionId);

  const headers = new Headers({ Accept: "application/json" });
  const apiToken = getApiToken();
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }
  if (policyRevision) {
    headers.set("X-DTX-Media-Policy-Revision", policyRevision);
  }

  const response = await fetch(`${getApiBaseUrl()}${endpoint}`, {
    method: "POST",
    credentials: "include",
    headers,
    body: formData,
    signal,
  });

  if (!response.ok) {
    throw new ImageUploadApiError(
      response.status,
      await parseUploadErrorBody(response),
      parseRetryAfterMs(response.headers.get("Retry-After")),
    );
  }

  const body = (await response.json()) as ImageUploadResponse;
  const data = body.data;
  if (
    !data?.asset_id ||
    !data.logical_ref ||
    !data.content_url ||
    !data.mime ||
    typeof data.bytes !== "number" ||
    !data.sha256
  ) {
    throw new Error("Upload response was incomplete.");
  }
  return data as ImageUploadResult;
}

async function parseUploadErrorBody(response: Response): Promise<ApiErrorBody> {
  const text = await response.text();
  if (text) {
    try {
      return JSON.parse(text) as ApiErrorBody;
    } catch {
      return fallbackUploadErrorBody(text);
    }
  }
  return fallbackUploadErrorBody(`Request failed with ${response.status}`);
}

function fallbackUploadErrorBody(message: string): ApiErrorBody {
  return {
    error_code: "internal_error",
    message,
    request_id: "",
    warnings: [],
  };
}

function parseRetryAfterMs(value: string | null): number | null {
  if (value === null) {
    return null;
  }

  const retryAfter = value.trim();
  if (/^\d+$/.test(retryAfter)) {
    const milliseconds = Number(retryAfter) * 1_000;
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }

  const retryAt = Date.parse(retryAfter);
  if (!Number.isFinite(retryAt)) {
    return null;
  }
  return Math.max(0, retryAt - Date.now());
}
