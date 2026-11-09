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

/** Set explicit workspace Labs flags for a feature fixture using the real policy API. */
export async function configureWorkspaceLabs(
  request: APIRequestContext,
  workspaceId: string,
  changes: { ask_enabled?: boolean; agent_enabled?: boolean; workspace_map_enabled?: boolean },
) {
  const endpoint = `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/settings/policies`;
  const current = await request.get(endpoint);
  expect(current.ok()).toBeTruthy();
  const revision = (await current.json()).data.labs.revision as string;
  const saved = await request.patch(`${endpoint}/batch`, {
    data: { group: "labs", expected_revision: revision, changes },
  });
  expect(saved.ok()).toBeTruthy();
}

/** Enable the current workspace map through its real Labs save form. */
export async function enableKnowledgeMapLab(page: Page) {
  const workspace = await currentWorkspace(page.request);
  await page.goto(`${workspaceUrl(workspace.id, "settings")}?section=labs`);
  await page.getByRole("checkbox", { name: /^(Workspace Knowledge Map|워크스페이스 지식 지도)$/ }).check();
  await saveLabsSettings(page, "graph");
}

/** Save the Labs draft and wait for the server to accept its revision. */
export async function saveLabsSettings(page: Page, group: "labs" | "graph" = "labs") {
  const checkbox = page.getByRole("checkbox", {
    name: group === "graph" ? /^(Workspace Knowledge Map|워크스페이스 지식 지도)$/ : /^Ask$/,
  });
  const card = page.locator(".policy-card").filter({ has: checkbox });
  const save = card.getByRole("button", { name: /^(Save|저장)$/ });
  if (await save.isEnabled()) {
    const saved = page.waitForResponse((response) => response.request().method() === "PATCH"
      && response.url().endsWith("/settings/policies/batch")
      && response.request().postDataJSON()?.group === group);
    await save.click();
    expect((await saved).ok()).toBeTruthy();
    await expect(save).toBeDisabled();
  }
}

/** Build a product URL whose workspace scope is explicit in its path. */
export function workspaceUrl(workspaceId: string, suffix: string) {
  return `/workspaces/${encodeURIComponent(workspaceId)}/${suffix.replace(/^\/+/, "")}`;
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
