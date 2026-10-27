import {
  expect,
  type APIRequestContext,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { createHash } from "node:crypto";

export const backendBaseUrl =
  process.env.DTXWIKI_BACKEND_BASE_URL ??
  `http://127.0.0.1:${process.env.DTXWIKI_BACKEND_PORT ?? "21080"}`;
// The backend serves the built SPA, so the app origin IS the API origin.
export const frontendBaseUrl =
  process.env.DTXWIKI_FRONTEND_BASE_URL ?? backendBaseUrl;
// Named only so single_origin.spec.ts can prove the browser never reaches it:
// `npm run dev` still serves the SPA here, but no E2E page may load from it.
export const viteDevServerOrigin =
  `http://127.0.0.1:${process.env.DTXWIKI_FRONTEND_DEV_PORT ?? "21173"}`;
export const e2ePassword = "correct-password";

export async function authenticatePage(
  page: Page,
  testInfo: TestInfo,
  label: string,
) {
  await signUp(page.request, uniqueEmail(testInfo, label));
}

export async function signUp(request: APIRequestContext, email: string) {
  const response = await request.post(`${backendBaseUrl}/api/v1/auth/signup`, {
    data: { email, password: e2ePassword },
  });
  expect(response.ok()).toBeTruthy();
}

export async function login(
  request: APIRequestContext,
  email: string,
  password = e2ePassword,
) {
  const response = await request.post(`${backendBaseUrl}/api/v1/auth/login`, {
    data: { email, password },
  });
  expect(response.ok()).toBeTruthy();
  return response;
}

export async function currentWorkspace(request: APIRequestContext) {
  const response = await request.get(`${backendBaseUrl}/api/v1/workspaces`);
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const data = body.data as {
    current_workspace_id: string | null;
    default_workspace_id?: string | null;
    workspaces: Array<{ id: string; slug: string; title: string }>;
  };
  const resolvedId = data.current_workspace_id ?? data.default_workspace_id;
  const workspace = data.workspaces.find(
    (candidate) => candidate.id === resolvedId,
  );
  expect(workspace, "authenticated user must have a resolved workspace").toBeTruthy();
  return workspace!;
}

export function seededAdminEmail(viewportKey: string, projectName: string) {
  return `e2e-admin-${projectName}-${viewportKey}@example.com`;
}

export function seededRecoveryEmail(viewportKey: string, projectName: string) {
  return `e2e-recovery-${projectName}-${viewportKey}@example.com`;
}

export function seededContextMediaOwnerEmail(projectName: string) {
  return `e2e-context-media-${projectName}@example.com`;
}

export function seededMcpAccessEmail(projectName: string) {
  return `e2e-mcp-access-${projectName}@example.com`;
}

export function seededPasswordResetStateToken(
  viewportKey: string,
  state: "expired" | "used",
) {
  const payload = createHash("sha256")
    .update(`dtxwiki-e2e-password-reset:${state}:${viewportKey}`)
    .digest("base64url");
  return `dtxr_0_${payload}`;
}

function uniqueEmail(testInfo: TestInfo, label: string) {
  const safeLabel = label.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
  return [
    "e2e",
    safeLabel,
    testInfo.workerIndex,
    testInfo.retry,
    Date.now(),
  ].join("-") + "@example.com";
}
