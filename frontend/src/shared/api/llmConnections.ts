import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type LlmAdapter = components["schemas"]["LLMAdapterView"];
export type LlmAdapterEndpoint = components["schemas"]["LLMAdapterEndpoint"];
export type LlmConnection = components["schemas"]["LLMConnectionView"];
export type LlmModelProfile = components["schemas"]["LLMModelProfile"];
export type LlmVerification = components["schemas"]["LLMVerificationView"];
export type LlmConnectionCreate = components["schemas"]["LLMConnectionCreate"];
export type LlmConnectionUpdate = components["schemas"]["LLMConnectionUpdate"];
export type LlmCredentialReplace = components["schemas"]["LLMCredentialReplace"];

/** Personal connections belong to the account; shared ones to a Workspace. */
export type LlmConnectionScope =
  | { kind: "user" }
  | { kind: "workspace"; workspaceId: string };

function base(scope: LlmConnectionScope) {
  return scope.kind === "user"
    ? "/api/v1/me"
    : `/api/v1/workspaces/${encodeURIComponent(scope.workspaceId)}`;
}

function connectionPath(scope: LlmConnectionScope, connectionId?: string) {
  const path = `${base(scope)}/llm-connections`;
  return connectionId ? `${path}/${encodeURIComponent(connectionId)}` : path;
}

export async function listLlmAdapters(signal?: AbortSignal) {
  const response = await apiRequest<{ data: LlmAdapter[] }>("/api/v1/llm-adapters", {
    signal,
  });
  return response.data;
}

export async function listLlmConnections(
  scope: LlmConnectionScope,
  signal?: AbortSignal,
) {
  const response = await apiRequest<{ data: LlmConnection[] }>(connectionPath(scope), {
    signal,
  });
  return response.data;
}

export async function createLlmConnection(
  scope: LlmConnectionScope,
  body: LlmConnectionCreate,
) {
  const response = await apiRequest<{ data: LlmConnection }>(connectionPath(scope), {
    method: "POST",
    body: JSON.stringify(body),
  });
  return response.data;
}

export async function updateLlmConnection(
  scope: LlmConnectionScope,
  connectionId: string,
  body: LlmConnectionUpdate,
) {
  const response = await apiRequest<{ data: LlmConnection }>(
    connectionPath(scope, connectionId),
    { method: "PATCH", body: JSON.stringify(body) },
  );
  return response.data;
}

export async function deleteLlmConnection(
  scope: LlmConnectionScope,
  connectionId: string,
  expectedRevision: number,
) {
  await apiRequest(
    `${connectionPath(scope, connectionId)}?expected_revision=${expectedRevision}`,
    { method: "DELETE" },
  );
}

/** Queue a check of the stored setup. Asking again returns the same check. */
export async function verifyLlmConnection(
  scope: LlmConnectionScope,
  connectionId: string,
  expectedRevision: number,
) {
  const response = await apiRequest<{ data: LlmVerification }>(
    `${connectionPath(scope, connectionId)}/verify`,
    { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
  );
  return response.data;
}

/** A new key replaces the current one only after its check succeeds. */
export async function replaceLlmCredential(
  scope: LlmConnectionScope,
  connectionId: string,
  body: LlmCredentialReplace,
) {
  const response = await apiRequest<{ data: LlmVerification }>(
    `${connectionPath(scope, connectionId)}/credential`,
    { method: "PUT", body: JSON.stringify(body) },
  );
  return response.data;
}

export async function cancelLlmVerification(
  scope: LlmConnectionScope,
  operationId: string,
) {
  const response = await apiRequest<{ data: LlmVerification }>(
    `${base(scope)}/llm-verifications/${encodeURIComponent(operationId)}/cancel`,
    { method: "POST" },
  );
  return response.data;
}

export function verificationInProgress(verification: LlmVerification | null | undefined) {
  return verification?.status === "queued" || verification?.status === "running";
}
