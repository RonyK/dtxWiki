import { apiRequest } from "./client";
import {
  MCP_ACCESS_PAGE_SIZE,
  pageQuery,
  type AccessKeyListStatus,
  type PageFilters,
} from "./mcpAccess";
import type { components } from "./types.gen";

export type AuthUser = {
  user_id: string;
  email: string;
  display_name: string;
  status: string;
  email_verified_at?: string | null;
  has_password?: boolean;
  session_method?: string | null;
  session_provider_slug?: string | null;
  // Instance (server) admin flag — populated authoritatively by GET /auth/me.
  // Gates system/deployment settings; absent on older/non-/me responses.
  is_server_admin?: boolean;
  // Authoritative MCP OAuth availability (https issuer only) from GET /auth/me;
  // the MCP-access UI uses this server truth instead of a client URL heuristic.
  oauth_available?: boolean;
  oauth_unavailable_reason?: string | null;
  // The canonical MCP endpoint under the server's configured public origin.
  // The screen shows and copies this rather than deriving one from the
  // browser's location, which would name whichever host an admin browsed to.
  mcp_url?: string | null;
  // MCP-10: advisory only -- DCR off does NOT make oauth_available false (a
  // client using a URL client identifier authorizes without DCR). Absent on
  // older/non-/me responses; treat undefined as "unknown, assume enabled"
  // the same way oauth_available's own default does.
  oauth_dcr_enabled?: boolean;
};

export type AuthSession = {
  user: AuthUser;
  expires_at: string;
};

export type ApiToken = components["schemas"]["ApiTokenData"];

export type ApiTokenIssue = components["schemas"]["ApiTokenIssueData"];

/**
 * One allowed workspace on a personal access token (D-01v2). Replaces the
 * single `workspace_id` a token used to be bound to; `sync_state` reports the
 * control -> workspace replica fan-out (`pending` / `synced` / `failed`).
 */
export type ApiTokenWorkspaceGrant =
  components["schemas"]["WorkspaceGrantData"];

export type ApiTokenWorkspaceGrantInput =
  components["schemas"]["WorkspaceGrantInput"];

type AuthSessionResponse = {
  data: AuthSession;
};

type AuthMeResponse = {
  data: AuthUser;
};

type AuthLogoutResponse = {
  data: {
    logged_out: boolean;
  };
};

type ReauthenticateResponse = {
  data: {
    reauthenticated_until: string;
  };
};

export type LoginMethod = {
  slug: string;
  method_id: string;
  protocol: string;
  display_name: string;
  label: { ko: string; en: string };
  icon_url: string | null;
  background?: string | null;
  foreground?: string | null;
  start_url: string;
  supports_reauthentication: boolean;
};

export type LoginIdentity = {
  id: string;
  provider_slug: string;
  last_email: string | null;
  last_login_at: string | null;
  can_unlink: boolean;
};

export type LoginIdentities = {
  has_password: boolean;
  identities: LoginIdentity[];
  available_providers: LoginMethod[];
};

type AuthCapabilitiesResponse = {
  data: {
    signup_enabled: boolean;
    password_login?: { enabled: boolean };
    login_methods?: LoginMethod[];
    password_recovery: {
      mode: "email" | "admin_assisted" | "disabled";
      admin_assisted: boolean;
    };
  };
};

type PasswordResetRequestResponse = {
  data: {
    requested: boolean;
    message: string;
  };
};

type EmailVerificationResponse = {
  data: {
    requested: boolean;
    verified: boolean;
  };
};

type PasswordResetConfirmResponse = {
  data: {
    reset: boolean;
  };
};

type PasswordResetStatusResponse = {
  data: {
    valid: boolean;
  };
};

// Exported since P10.3 T5: the account-scoped Settings listing
// (`listAccessKeys`) hands this shape to the MCP access screen.
export type ApiTokenListResponse =
  components["schemas"]["ApiTokenListResponse"];

type ApiTokenIssueResponse = components["schemas"]["ApiTokenIssueResponse"];

type ApiTokenRevokeResponse = components["schemas"]["ApiTokenRevokeResponse"];

type ApiTokenDeleteResponse = components["schemas"]["ApiTokenDeleteResponse"];

// `remember_me` is required, not optional: the server now fails closed and
// treats an omitted flag as a plain browser session, so a caller that leaves it
// out silently opts out of "keep me signed in" instead of inheriting it.
export function signup(input: {
  email: string;
  password: string;
  display_name?: string;
  remember_me: boolean;
}) {
  return apiRequest<AuthSessionResponse>("/api/v1/auth/signup", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

export function login(input: {
  email: string;
  password: string;
  remember_me: boolean;
}) {
  return apiRequest<AuthSessionResponse>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

export function logout() {
  return apiRequest<AuthLogoutResponse>("/api/v1/auth/logout", {
    method: "POST",
  }).then((response) => response.data);
}

export function getMe(signal?: AbortSignal) {
  return apiRequest<AuthMeResponse>("/api/v1/auth/me", { signal }).then(
    (response) => response.data,
  );
}

export function changePassword(input: {
  current_password: string;
  new_password: string;
}) {
  return apiRequest<AuthSessionResponse>("/api/v1/auth/password", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

export function reauthenticate(input: { current_password: string }) {
  return apiRequest<ReauthenticateResponse>("/api/v1/auth/reauthenticate", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

export function getAuthCapabilities() {
  return apiRequest<AuthCapabilitiesResponse>("/api/v1/auth/capabilities").then(
    (response) => response.data,
  );
}

export function getLoginIdentities() {
  return apiRequest<{ data: LoginIdentities }>("/api/v1/auth/identities").then(
    (response) => response.data,
  );
}

export function startIdentityFlow(slug: string, action: "link" | "reauthenticate", returnTo: string) {
  return apiRequest<{ data: { redirect_url: string } }>(
    `/api/v1/auth/sso/${encodeURIComponent(slug)}/${action}`,
    { method: "POST", body: JSON.stringify({ return_to: returnTo }) },
  ).then((response) => response.data);
}

export function unlinkLoginIdentity(id: string) {
  return apiRequest<AuthSessionResponse>(`/api/v1/auth/identities/${encodeURIComponent(id)}`, {
    method: "DELETE",
  }).then((response) => response.data);
}

export function setAccountPassword(newPassword: string) {
  return apiRequest<AuthSessionResponse>("/api/v1/auth/password/set", {
    method: "POST", body: JSON.stringify({ new_password: newPassword }),
  }).then((response) => response.data);
}

export function confirmPasswordReset(input: {
  token: string;
  new_password: string;
  revoke_api_tokens?: boolean;
}) {
  return apiRequest<PasswordResetConfirmResponse>(
    "/api/v1/auth/password-reset/confirm",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  ).then((response) => response.data);
}

/**
 * Read-only check of a password-reset token. A POST with the token in the JSON
 * body: the request line of a GET reaches every access log on the path, and a
 * reset token is a credential until it is used or expires.
 */
export function checkPasswordResetStatus(token: string) {
  return apiRequest<PasswordResetStatusResponse>(
    "/api/v1/auth/password-reset/status",
    {
      method: "POST",
      body: JSON.stringify({ token }),
    },
  ).then((response) => response.data);
}

export function requestPasswordReset(input: { email: string }) {
  return apiRequest<PasswordResetRequestResponse>(
    "/api/v1/auth/password-reset/request",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  ).then((response) => response.data);
}

export function requestEmailVerification() {
  return apiRequest<EmailVerificationResponse>(
    "/api/v1/auth/email-verification/request",
    { method: "POST" },
  ).then((response) => response.data);
}

export function confirmEmailVerification(input: { token: string }) {
  return apiRequest<EmailVerificationResponse>(
    "/api/v1/auth/email-verification/confirm",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  ).then((response) => response.data);
}

export function listApiTokens() {
  return apiRequest<ApiTokenListResponse>("/api/v1/auth/tokens").then(
    (response) => response.data,
  );
}

/**
 * `GET /api/v1/auth/tokens` is the canonical Settings access-key listing since
 * D-01v2 (P10.3 T4). It carries the same status/search/cursor projection the
 * deleted workspace-scoped `settings/access-keys` route used to, but over the
 * whole account rather than one workspace.
 */
export function listAccessKeys(
  filters: PageFilters<AccessKeyListStatus>,
  signal?: AbortSignal,
) {
  return apiRequest<ApiTokenListResponse>(
    `/api/v1/auth/tokens${pageQuery({
      ...filters,
      limit: filters.limit ?? MCP_ACCESS_PAGE_SIZE,
    })}`,
    { signal },
  );
}

export function createApiToken(input: {
  /**
   * At least one allowed workspace. `scopes` is omitted per grant by this UI:
   * the MVP applies the token's ceiling `scopes` to every selected workspace
   * (D-01v3-f; per-row scope editing is a follow-up).
   */
  workspace_grants: ApiTokenWorkspaceGrantInput[];
  name: string;
  scopes: string[];
  /**
   * Omitted entirely means "server default"; an explicit `null` means no
   * expiry. Do not coerce one into the other.
   */
  expires_in_days?: number | null;
  catalog_app_id?: string | null;
  custom_app_label?: string | null;
  mcp_client_type?: string | null;
}) {
  return apiRequest<ApiTokenIssueResponse>("/api/v1/auth/tokens", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

export function renameApiToken(tokenId: string, input: { name: string }) {
  return apiRequest<ApiTokenRevokeResponse>(`/api/v1/auth/tokens/${tokenId}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  }).then((response) => response.data);
}

/** Full-set replace of a token's allowed workspaces (>= 1). */
export function updateApiTokenWorkspaces(
  tokenId: string,
  workspaceIds: string[],
) {
  return apiRequest<ApiTokenRevokeResponse>(`/api/v1/auth/tokens/${tokenId}`, {
    method: "PATCH",
    body: JSON.stringify({
      workspace_grants: workspaceIds.map((workspace_id) => ({ workspace_id })),
    }),
  }).then((response) => response.data);
}

export function revokeApiToken(tokenId: string) {
  return apiRequest<ApiTokenRevokeResponse>(`/api/v1/auth/tokens/${tokenId}`, {
    method: "DELETE",
  }).then((response) => response.data);
}

export function deleteRevokedApiToken(tokenId: string) {
  return apiRequest<ApiTokenDeleteResponse>(
    `/api/v1/auth/tokens/${tokenId}/record`,
    {
      method: "DELETE",
    },
  ).then((response) => response.data);
}
