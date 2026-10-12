import { expect, test } from "./fixtures";

import {
  authenticatePage,
  backendBaseUrl,
  currentWorkspace,
  signUp,
} from "./auth";

test("requires login at the root route", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/login\?next=%2F$/);
  await expect(page.getByRole("heading", { name: "Sign In" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Sign up" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);
});

test("shows the dashboard at the authenticated root route", async ({
  page,
}, testInfo) => {
  await authenticatePage(page, testInfo, "landing");
  await page.goto("/");

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(
    page.getByRole("heading", { name: "Workspace Operations" }),
  ).toBeVisible();
  await page.getByRole("banner").getByRole("button", { name: /Create/ }).click();
  await expect(
    page.getByRole("menuitem", { name: "New Context page" }),
  ).toHaveAttribute("href", "/context/new?from=context");
});

test("creates, reads, edits, and shows stale conflict in the browser @cross-browser", async ({
  page,
}, testInfo) => {
  const request = page.request;
  await signUp(
    request,
    `pages-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  await page.goto("/context/new?from=context");
  const workspace = await currentWorkspace(request);

  await page.getByLabel("Title").fill("Hello PoC");
  await expect(page.getByLabel("Title")).toHaveValue("Hello PoC");
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText("# Hello PoC\n\n- [x] rendered");
  await expect(page.locator(".cm-content")).toContainText("rendered");
  const createResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/v1/workspaces/${workspace.id}/context/pages` &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save draft" }).click();
  const created = await createResponse;
  expect(created.ok()).toBeTruthy();
  const createdPage = (await created.json()).data as { id: string; slug: string };

  await expect(page).toHaveURL(
    new RegExp(`/context/${createdPage.slug}/edit$`),
  );
  await expect(page.getByRole("button", { name: "Publish" })).toBeEnabled();
  const publishResponse = page.waitForResponse(
    (response) =>
      new RegExp(
        `/api/v1/workspaces/${workspace.id}/context/pages/[^/]+/versions/[^/]+/publish$`,
      ).test(new URL(response.url()).pathname) &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Publish" }).click();
  expect((await publishResponse).ok()).toBeTruthy();
  await expect(page).toHaveURL(
    new RegExp(`/context\\?target=${createdPage.id}$`),
  );
  await expect(page.getByRole("main").getByRole("heading", { name: "Hello PoC" }))
    .toBeVisible();
  await expect(page.getByRole("article")).toContainText("rendered");
  await expect(page).toHaveTitle("Hello PoC - dtxWiki");
  const favicon = page.locator('link[rel="icon"]');
  await expect(favicon).toHaveAttribute("type", "image/svg+xml");
  const faviconUrl = await favicon.getAttribute("href");
  expect(faviconUrl).toMatch(/^\/assets\/dtx-dock-icon-[^/]+\.svg$/);
  const faviconResponse = await request.get(faviconUrl!);
  expect(faviconResponse.ok()).toBeTruthy();
  expect(faviconResponse.headers()["content-type"]).toContain("image/svg+xml");
  expect(await faviconResponse.text()).toContain('viewBox="0 0 256 256"');

  // The former ambient-workspace /pages/{slug} route was deliberately removed.
  // Exercise the stable, workspace-qualified Context permalink instead.
  await page.goto(
    `/workspaces/${workspace.id}/context/pages/${createdPage.id}?from=canonical-e2e`,
  );
  await expect(page).toHaveURL(
    new RegExp(
      `/workspaces/${workspace.id}/context/pages/${createdPage.id}\\?from=canonical-e2e$`,
    ),
  );
  await expect(page.getByRole("main").getByRole("heading", { name: "Hello PoC" }))
    .toBeVisible();
  const article = page.getByRole("article");
  await expect(article.getByText("rendered")).toBeVisible();
  await expect(page).toHaveTitle("Hello PoC - dtxWiki");

  await page.goto(`/context/${createdPage.slug}/edit`);
  await expect(page.getByLabel("Title")).toHaveValue("Hello PoC");
  await expect(page).toHaveTitle("dtxWiki");

  const readResponse = await request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${createdPage.id}`,
  );
  const current = (await readResponse.json()).data;

  await request.patch(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${createdPage.id}`,
    {
      data: {
        title: "Hello PoC",
        body_md: "# Server update",
        base_version_id: current.version_id,
        base_content_hash: current.content_hash,
      },
    },
  );

  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText("# Browser stale update");
  await page.getByRole("button", { name: "Save draft" }).click();

  await expect(page.getByText(/Someone else updated this page/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Reload latest" })).toBeVisible();
});

test("publishes a new Context page directly to its reader", async ({ page }, testInfo) => {
  const request = page.request;
  await signUp(
    request,
    `direct-publish-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  const workspace = await currentWorkspace(request);
  await page.goto("/context/new?from=context");

  await page.getByLabel("Title").fill("Direct publish PoC");
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText("# Direct publish PoC\n\nPublished without saving a draft first.");

  const createResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/v1/workspaces/${workspace.id}/context/pages` &&
      response.request().method() === "POST",
  );
  const publishResponse = page.waitForResponse(
    (response) =>
      new RegExp(
        `/api/v1/workspaces/${workspace.id}/context/pages/[^/]+/versions/[^/]+/publish$`,
      ).test(new URL(response.url()).pathname) &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  const created = await createResponse;
  expect(created.ok()).toBeTruthy();
  const createdPage = (await created.json()).data as { id: string };
  expect((await publishResponse).ok()).toBeTruthy();
  await expect(page).toHaveURL(
    new RegExp(`/context\\?target=${createdPage.id}$`),
  );
  await expect(
    page.getByRole("main").getByRole("heading", { name: "Direct publish PoC" }),
  ).toBeVisible();
  await expect(page.getByRole("article")).toContainText(
    "Published without saving a draft first.",
  );
});

test("uploads an image through the slash image command in the browser @cross-browser", async ({
  page,
}, testInfo) => {
  const request = page.request;
  await signUp(
    request,
    `slash-image-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  const workspace = await currentWorkspace(request);
  const createResponse = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        title: "Slash image upload",
        body_md: "# Slash image upload\n\nStart here.",
        page_type: "concept",
        tags: ["e2e"],
      },
    },
  );
  expect(createResponse.ok()).toBeTruthy();
  const created = (await createResponse.json()).data as { slug: string };

  await page.goto(`/context/${created.slug}/edit`);
  await page.getByLabel("Markdown body").waitFor();
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+/");
  const menu = page.getByRole("dialog", { name: "Insert block" });
  await menu.getByLabel("Filter block commands").fill("image");

  const uploadResponse = page.waitForResponse(
    (response) =>
      new RegExp(
        `/api/v1/workspaces/${workspace.id}/context/pages/[^/]+/media$`,
      ).test(new URL(response.url()).pathname) &&
      response.request().method() === "POST",
  );
  const fileChooser = page.waitForEvent("filechooser");
  await menu.getByRole("option", { name: "Image" }).click();
  await (await fileChooser).setFiles({
    name: "slash-e2e.png",
    mimeType: "image/png",
    // A real 2x2 PNG, not the widely copy-pasted 1x1 transparent blob: that
    // blob carries a bad IDAT CRC, so Pillow's verify() rejects it as
    // "broken PNG file (bad header checksum)". validate_context_image runs
    // verify() before load() precisely to keep corrupt uploads out, so the old
    // fixture made this test assert that a damaged file uploads successfully.
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR42mOssXrLwMDAxMDAwMDAAAATXQGnHH/cKwAAAABJRU5ErkJggg==",
      "base64",
    ),
  });
  const uploaded = await uploadResponse;
  expect(uploaded.ok()).toBeTruthy();
  const upload = (await uploaded.json()).data as { logical_ref: string };

  const editor = page.locator(".cm-content");
  await expect(editor).toContainText(`![slash-e2e.png](${upload.logical_ref})`);
  await expect(editor).not.toContainText("image-url");
});
