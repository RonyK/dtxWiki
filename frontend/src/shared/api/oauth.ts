import { apiRequest } from "./client";
import type { components } from "./types.gen";

// The backend types all of this, so it is derived rather than restated here.
export type OAuthConsent = components["schemas"]["OAuthConsentData"];
export type ConnectionState = OAuthConsent["connection_state"];
/**
 * One allowed workspace on a (user, client) consent group. Since D-01v2 a
 * consent row is one (user, client) pair holding a SET of workspaces, not one
 * (user, client, workspace) triple.
 */
export type OAuthConsentWorkspace =
  components["schemas"]["OAuthConsentWorkspaceData"];

type OAuthConsentListResponse =
  components["schemas"]["OAuthConsentListResponse"];
type OAuthConsentRevokeResponse =
  components["schemas"]["OAuthConsentRevokeResponse"];
type OAuthConsentSettingsUpdateRequest =
  components["schemas"]["OAuthConsentSettingsUpdateRequest"];
type OAuthConsentSettingsUpdateResponse =
  components["schemas"]["OAuthConsentSettingsUpdateResponse"];

export type OAuthConsentPage = {
  data: OAuthConsent[];
  next_cursor: string | null;
};

export type OAuthConnectionCleanup =
  components["schemas"]["OAuthConnectionCleanupData"];

export function listOAuthConsentPage({
  cursor,
  limit,
}: { cursor?: string | null; limit?: number } = {}): Promise<OAuthConsentPage> {
  const query = new URLSearchParams();
  if (limit !== undefined) {
    query.set("limit", String(limit));
  }
  if (cursor) {
    query.set("cursor", cursor);
  }
  const suffix = query.size > 0 ? `?${query}` : "";
  return apiRequest<OAuthConsentListResponse>(`/oauth/consents${suffix}`).then(
    (response) => ({
      data: response.data,
      next_cursor: response.next_cursor ?? null,
    }),
  );
}

export function listOAuthConsents() {
  return listOAuthConsentPage().then((response) => response.data);
}

export function revokeOAuthConsent(clientId: string) {
  const query = new URLSearchParams({ client_id: clientId });
  return apiRequest<OAuthConsentRevokeResponse>(
    `/oauth/consents?${query}`,
    { method: "DELETE" },
  ).then((response) => response.data);
}

/**
 * Patches one (user, client) consent group. `workspace_ids` in the payload is a
 * full-set replace of the allowed workspaces (>= 1); the TTL/sliding fields are
 * group-wide as before. The query carries `client_id` only — there is no
 * per-workspace consent row to address any more (D-01v2, section 2.3).
 */
export function updateOAuthConsentSettings(
  consent: OAuthConsent,
  payload: OAuthConsentSettingsUpdateRequest,
) {
  const query = new URLSearchParams({ client_id: consent.client_id });
  return apiRequest<OAuthConsentSettingsUpdateResponse>(
    `/oauth/consents?${query}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  ).then((response) => response.data);
}

export function previewOAuthConnectionCleanup() {
  return apiRequest<{ data: OAuthConnectionCleanup }>(
    "/oauth/consents/cleanup",
  ).then((response) => response.data);
}

export function cleanupOAuthConnections() {
  return apiRequest<{ data: OAuthConnectionCleanup }>(
    "/oauth/consents/cleanup",
    { method: "POST" },
  ).then((response) => response.data);
}
