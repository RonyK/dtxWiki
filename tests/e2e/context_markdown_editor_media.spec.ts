import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
  type Request,
  type TestInfo,
} from "./fixtures";
import { randomUUID } from "node:crypto";

import {
  backendBaseUrl,
  currentWorkspace,
  frontendBaseUrl,
  login,
  seededContextMediaOwnerEmail,
  signUp,
  workspaceUrl,
} from "./auth";
import { EDITOR_COMMANDS } from "../../frontend/src/features/pages/editorCommands";
import { parsePageDocument } from "../../frontend/src/features/pages/pageFrontmatter";
import {
  evidenceLocatorScreenshot,
  evidenceScreenshot,
} from "./evidence";

const PNG_BUFFER = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAAFUlEQVR4nGOUq7jznwEPYMInSR0FAD1JAnlVwrsyAAAAAElFTkSuQmCC",
  "base64",
);
const JPEG_BUFFER = Buffer.from(
  "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAEAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCnRRRXyZ+hH//Z",
  "base64",
);
const ANIMATED_GIF_BUFFER = Buffer.from(
  "R0lGODlhAwACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAAAwACAAAIBgABCBwYEAAh+QQBCgABACwAAAAAAwACAIEAAP8AAAAAAAAAAAAIBgABCBwYEAA7",
  "base64",
);
const ANIMATED_WEBP_BUFFER = Buffer.from(
  "UklGRoQAAABXRUJQVlA4WAoAAAACAAAAAgAAAQAAQU5JTQYAAAAAAAAAAABBTk1GKAAAAAAAAAAAAAIAAAEAAGQAAAJWUDhMDwAAAC8CQAAABxD9j/4HIqL/AQBBTk1GKAAAAAAAAAAAAAIAAAEAAGQAAABWUDhMDwAAAC8CQAAABxDR//4HIqL/AQA=",
  "base64",
);

const UPLOADS = [
  { name: "a-first.png", mimeType: "image/png", buffer: PNG_BUFFER },
  { name: "b-retry.jpg", mimeType: "image/jpeg", buffer: JPEG_BUFFER },
  { name: "c-third.png", mimeType: "image/png", buffer: PNG_BUFFER },
  { name: "d-active-cancel.jpg", mimeType: "image/jpeg", buffer: JPEG_BUFFER },
  { name: "e-fifth.png", mimeType: "image/png", buffer: PNG_BUFFER },
] as const;
const FAILED_NAME = UPLOADS[1].name;
const CANCELLED_NAME = UPLOADS[3].name;
const RETAINED_UPLOAD_NAMES = [
  UPLOADS[0].name,
  UPLOADS[1].name,
  UPLOADS[2].name,
  UPLOADS[4].name,
] as const;
// P8/P9.1: the media-session batch-upload bridge this route used to mock was
// deleted -- each file now uploads immediately to the page's own (page-first)
// media route (see PageEditor.tsx's createUploadTargetSnapshot).
const SESSION_UPLOAD_ROUTE =
  "**/api/v1/workspaces/*/context/pages/*/media";
const UUID_PATTERN =
  "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ACTION_BAR_COMMAND_IDS = EDITOR_COMMANDS
  .filter((command) =>
    (command.surfaces as readonly string[]).includes("action-bar"),
  )
  .map((command) => command.id)
  .sort();
const SLASH_COMMAND_IDS = EDITOR_COMMANDS
  .filter((command) =>
    (command.surfaces as readonly string[]).includes("slash"),
  )
  .map((command) => command.id)
  .sort();
const VERIFIED_OWNER_VIEWPORT_KEYS = {
  chromium: "1280x720",
  firefox: "1440x900",
  webkit: "1920x1080",
} as const;

const EDITOR_CHROME_LABELS = {
  en: {
    attach: "Attach files",
    filterBlocks: "Filter block commands",
    insertBlock: "Insert block",
    more: "More editor actions",
    reading: "Reading",
    source: "Source",
    toolbar: "Editor actions",
  },
  ko: {
    attach: "파일 첨부",
    filterBlocks: "블록 명령 필터",
    insertBlock: "블록 삽입",
    more: "편집기 작업 더 보기",
    reading: "읽기",
    source: "소스",
    toolbar: "편집기 작업",
  },
} as const;

type PagePayload = {
  id: string;
  slug: string;
  title: string;
  body_md: string;
  version_id: string;
  content_hash: string;
  editing_revision: number;
  current_published_version_id: string | null;
};

type ContextMediaAsset = {
  id: string;
  logical_ref: string;
  usage_state: "active" | "history_only" | "unused";
  latest_draft_reference_count: number;
  current_published_reference_count: number;
  historical_reference_count: number;
  can_delete: boolean;
  usage_token: string;
};

type ContextMediaUploadPayload = {
  asset_id: string;
  logical_ref: string;
  content_url: string;
  mime: "image/gif" | "image/webp";
  bytes: number;
  sha256: string;
};

type GeometrySample = {
  alt: string;
  containerWidth: number;
  height: number;
  naturalHeight: number;
  naturalWidth: number;
  path: string;
  width: number;
};

test("publishes real GIF and animated WebP uploads through the trusted Context reader", async ({
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  expect(webpAnimationFrameCount(ANIMATED_WEBP_BUFFER)).toBe(2);

  await signUp(
    page.request,
    `context-gif-webp-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  const workspace = await currentWorkspace(page.request);
  const capabilitiesResponse = await page.request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/media/capabilities`,
  );
  expect(capabilitiesResponse.ok()).toBeTruthy();
  const capabilities = (await capabilitiesResponse.json()).data as {
    policy_revision: string;
    uploadable_media_types: string[];
  };
  expect(capabilities.uploadable_media_types).toEqual(
    expect.arrayContaining(["image/gif", "image/webp"]),
  );

  const suffix = `${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}`;
  const title = `Animated Context images ${suffix}`;
  const createResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        title,
        body_md: `# ${title}\n\nUpload fixtures before referencing them.`,
        page_type: "concept",
        tags: ["e2e"],
      },
    },
  );
  expect(createResponse.ok()).toBeTruthy();
  const created = (await createResponse.json()).data as PagePayload;

  const fixtures = [
    {
      alt: "Animated GIF fixture",
      buffer: ANIMATED_GIF_BUFFER,
      mime: "image/gif" as const,
      name: "animated-fixture.gif",
    },
    {
      alt: "Animated WebP fixture",
      buffer: ANIMATED_WEBP_BUFFER,
      mime: "image/webp" as const,
      name: "animated-fixture.webp",
    },
  ];
  const uploads: ContextMediaUploadPayload[] = [];
  for (const fixture of fixtures) {
    const uploadResponse = await page.request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/media`,
      {
        headers: {
          "X-DTX-Media-Policy-Revision": capabilities.policy_revision,
        },
        multipart: {
          file: {
            name: fixture.name,
            mimeType: fixture.mime,
            buffer: fixture.buffer,
          },
          marker_uuid: randomUUID(),
          editing_version_id: created.version_id,
        },
      },
    );
    expect(uploadResponse.ok(), await uploadResponse.text()).toBeTruthy();
    const upload = (await uploadResponse.json()).data as ContextMediaUploadPayload;
    expect(upload.mime).toBe(fixture.mime);
    expect(upload.bytes).toBe(fixture.buffer.length);
    expect(upload.logical_ref).toMatch(
      new RegExp(`^dtx-media://asset/${UUID_PATTERN}$`, "i"),
    );
    uploads.push(upload);
  }

  const body = [
    `# ${title}`,
    "",
    ...fixtures.flatMap((fixture, index) => [
      `![${fixture.alt}](${uploads[index].logical_ref})`,
      "",
    ]),
  ].join("\n");
  const autosaveResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/autosave`,
    {
      data: {
        title,
        body_md: body,
        base_version_id: created.version_id,
        base_content_hash: created.content_hash,
        expected_editing_revision: created.editing_revision,
        idempotency_key: randomUUID(),
      },
    },
  );
  expect(autosaveResponse.ok(), await autosaveResponse.text()).toBeTruthy();
  const draft = (await autosaveResponse.json()).data as PagePayload;
  const submitResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/submit-for-review`,
    {
      data: {
        version_id: draft.version_id,
        expected_editing_revision: draft.editing_revision,
        idempotency_key: randomUUID(),
      },
    },
  );
  expect(submitResponse.ok(), await submitResponse.text()).toBeTruthy();
  const submitted = (await submitResponse.json()).data as PagePayload & {
    review_item_id: string;
  };
  const decisionResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/reviews/${submitted.review_item_id}/decision`,
    { data: { decision_type: "approve" } },
  );
  expect(decisionResponse.ok(), await decisionResponse.text()).toBeTruthy();
  const published = await publishVersion(page.request, workspace.id, created.id, submitted);
  expect(published.current_published_version_id).toBe(submitted.version_id);

  const contentPaths = uploads.map(
    (upload) =>
      `/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/versions/${published.version_id}/media/${upload.asset_id}/content`,
  );
  for (const [index, contentPath] of contentPaths.entries()) {
    const contentResponse = await page.request.get(
      `${backendBaseUrl}${contentPath}`,
    );
    expect(contentResponse.ok()).toBeTruthy();
    expect(contentResponse.headers()["content-type"]).toBe(fixtures[index].mime);
    expect(await contentResponse.body()).toEqual(fixtures[index].buffer);
  }

  await page.goto(workspaceUrl(workspace.id, `context/pages/${created.id}`));
  await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
  const reader = page
    .getByRole("tabpanel", { name: /^View/ })
    .locator(".markdown-body");
  const images = reader.locator("img.markdown-image");
  await expect(images).toHaveCount(fixtures.length);
  for (let index = 0; index < fixtures.length; index += 1) {
    await images.nth(index).scrollIntoViewIfNeeded();
  }
  await expect
    .poll(() =>
      images.evaluateAll((elements) =>
        elements.map((element) => {
          const image = element as HTMLImageElement;
          return {
            complete: image.complete,
            naturalHeight: image.naturalHeight,
            naturalWidth: image.naturalWidth,
          };
        }),
      ),
    )
    .toEqual(
      fixtures.map(() => ({
        complete: true,
        naturalHeight: 2,
        naturalWidth: 3,
      })),
    );
  expect(
    await images.evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("src")),
    ),
  ).toEqual(contentPaths);
  expect(
    await images.evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("alt")),
    ),
  ).toEqual(fixtures.map((fixture) => fixture.alt));
});

test("preserves a five-image batch through retry, publication, revocation, and retained history", async ({
  browser,
  page,
}, testInfo) => {
  test.setTimeout(180_000);
  const suffix = [
    testInfo.project.name,
    testInfo.workerIndex,
    testInfo.retry,
    Date.now(),
  ]
    .join("-")
    .replace(/[^a-z0-9-]+/gi, "-")
    .toLowerCase();
  const title = `Context media critical ${suffix}`;
  const viewerEmail = `context-media-viewer-${suffix}@example.com`;
  const editorEmail = `context-media-editor-${suffix}@example.com`;
  const viewerContext = await browser.newContext({ baseURL: frontendBaseUrl });
  const editorContext = await browser.newContext({ baseURL: frontendBaseUrl });
  const anonymousContext = await browser.newContext({ baseURL: frontendBaseUrl });
  const activeCancelStarted = deferred();
  const releaseActiveCancel = deferred();
  const uploadAttempts = new Map<string, number>();
  const uploadTrace: string[] = [];

  try {
    const ownerViewportKey =
      VERIFIED_OWNER_VIEWPORT_KEYS[
        testInfo.project.name as keyof typeof VERIFIED_OWNER_VIEWPORT_KEYS
      ];
    expect(
      ownerViewportKey,
      `project ${testInfo.project.name} must have an isolated verified owner fixture`,
    ).toBeTruthy();
    await login(
      page.request,
      seededContextMediaOwnerEmail(testInfo.project.name),
    );
    await signUp(viewerContext.request, viewerEmail);
    await signUp(editorContext.request, editorEmail);
    const workspace = await currentWorkspace(page.request);
    const viewerId = await currentUserId(viewerContext.request);
    const editorId = await currentUserId(editorContext.request);
    await addWorkspaceMember(
      page.request,
      workspace.id,
      viewerEmail,
      "viewer",
    );
    await addWorkspaceMember(
      page.request,
      workspace.id,
      editorEmail,
      "editor",
    );
    await makeWorkspacePublic(page.request, workspace.id);

    await page.route(SESSION_UPLOAD_ROUTE, async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      const filename = multipartFilename(route.request());
      if (!filename || !UPLOADS.some((upload) => upload.name === filename)) {
        await route.abort("failed");
        return;
      }
      const attempt = (uploadAttempts.get(filename) ?? 0) + 1;
      uploadAttempts.set(filename, attempt);
      uploadTrace.push(filename);

      if (filename === FAILED_NAME && attempt === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          headers: {
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Origin": frontendBaseUrl,
          },
          body: JSON.stringify({
            error_code: "media_install_unavailable",
            message: "Deterministic E2E first-attempt failure.",
            request_id: `e2e-${suffix}`,
            warnings: [],
          }),
        });
        return;
      }
      if (filename === CANCELLED_NAME && attempt === 1) {
        activeCancelStarted.resolve();
        await releaseActiveCancel.promise;
        await route.abort("aborted").catch(() => undefined);
        return;
      }
      await route.continue();
    });

    await page.addInitScript(() => {
      window.localStorage.setItem("dtxwiki.uiLanguage", "en");
    });

    await test.step("expose one registry-driven ActionBar and slash surface with EN/KO keyboard focus", async () => {
      await page.goto(workspaceUrl(workspace.id, "context/pages/new"));
      const editor = page.locator(".cm-content");
      await expect(editor).toBeVisible();

      await expectEditorChrome(page, EDITOR_CHROME_LABELS.en);
      await captureEvidence(page, testInfo, "editor-actionbar-desktop");
      await page
        .getByRole("button", { name: "Switch language to 한국어" })
        .click();
      await expect(page.locator("html")).toHaveAttribute("lang", "ko");
      await expectEditorChrome(page, EDITOR_CHROME_LABELS.ko);
      await page
        .getByRole("button", { name: /English.*언어 변경/ })
        .click();
      await expect(page.locator("html")).toHaveAttribute("lang", "en");

      await editor.click();
      await page.keyboard.insertText("registry parity");
      const platformModifier = await codeMirrorModifier(page);
      await page.keyboard.press(`${platformModifier}+A`);
      await page.keyboard.press(`${platformModifier}+B`);
      await expect(editor).toHaveText("**registry parity**");
      await page
        .getByRole("toolbar", { name: EDITOR_CHROME_LABELS.en.toolbar })
        .locator('[data-command-id="bold"]')
        .click();
      await expect(editor).toHaveText("registry parity");
      await editor.click();
      await page.keyboard.press(`${platformModifier}+A`);
      await page.keyboard.press("Backspace");
    });

    await test.step("keep the editor usable at 390x844 and at the 200 percent zoom CSS-viewport equivalent", async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await expect(page.locator(".editor-action-bar-shell")).toHaveAttribute(
        "data-viewport",
        "mobile",
      );
      // Matches editor.openExplorer, the key PageEditor actually renders. The
      // former "Open Context navigation" wording came from editor.rail.open-
      // Context, an orphaned key with no consumer (now removed), so this
      // assertion could never have matched the real button.
      const drawerToggle = page.getByRole("button", {
        name: "Open Context explorer",
      });
      const drawer = page.locator("#page-editor-context-rail");
      await expect(drawerToggle).toBeVisible();
      await expect(drawer).toHaveAttribute("data-mobile-open", "false");
      await expect(drawer).toHaveAttribute("aria-hidden", "true");
      await expect(drawer).toHaveAttribute("inert", "");
      await drawerToggle.click();
      await expect(drawer).toHaveAttribute("data-mobile-open", "true");
      await expect(drawer).toHaveAttribute("role", "dialog");
      await expect(drawer).toHaveAttribute("aria-modal", "true");
      await expect(drawer).not.toHaveAttribute("inert", "");
      expect(
        await drawer.evaluate((element) =>
          element.contains(document.activeElement),
        ),
      ).toBe(true);
      await page.keyboard.press("Escape");
      await expect(drawer).toHaveAttribute("data-mobile-open", "false");
      await expect(drawer).toHaveAttribute("aria-hidden", "true");
      await expect(drawer).toHaveAttribute("inert", "");
      await expect(drawerToggle).toBeFocused();
      await expect
        .poll(() =>
          drawer.evaluate((element) => element.getBoundingClientRect().right),
        )
        .toBeLessThanOrEqual(1);
      await captureEvidence(page, testInfo, "editor-mobile-390");

      const editorViewport = await page.locator(".editor-grid").evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return {
          height: rect.height,
          top: rect.top,
          visibleHeight: Math.max(
            0,
            Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0),
          ),
          viewportHeight: window.innerHeight,
        };
      });
      expect(editorViewport.top).toBeLessThan(editorViewport.viewportHeight);
      expect(editorViewport.height).toBeGreaterThanOrEqual(
        editorViewport.viewportHeight * 0.5 - 0.5,
      );
      expect(editorViewport.visibleHeight).toBeGreaterThanOrEqual(
        editorViewport.viewportHeight * 0.5 - 0.5,
      );
      await expectDocumentContainment(page);
      const mobileToolbar = page.getByRole("toolbar", {
        name: EDITOR_CHROME_LABELS.en.toolbar,
      });
      const mobileAttach = mobileToolbar.getByRole("button", {
        name: EDITOR_CHROME_LABELS.en.attach,
        exact: true,
      });
      await expect(mobileToolbar).toBeInViewport();
      await expect(mobileAttach).toBeVisible();
      await expect(mobileAttach).toBeInViewport();
      const mobileChromeGeometry = await page.locator(".editor-command-bar").evaluate((bar) => {
        const sample = (element: Element | null) => {
          if (!(element instanceof HTMLElement)) return null;
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return {
            bottom: rect.bottom,
            display: style.display,
            height: rect.height,
            left: rect.left,
            opacity: style.opacity,
            overflow: style.overflow,
            right: rect.right,
            top: rect.top,
            visibility: style.visibility,
            width: rect.width,
          };
        };
        return {
          actionBar: sample(bar.querySelector(".editor-action-bar")),
          actionBarShell: sample(bar.querySelector(".editor-action-bar-shell")),
          attach: sample(bar.querySelector('[data-command-id="attach"]')),
          commandBar: sample(bar),
          modeTabs: sample(bar.querySelector(".editor-mode-tabs")),
        };
      });
      expect(mobileChromeGeometry.actionBar).not.toBeNull();
      expect(mobileChromeGeometry.commandBar).not.toBeNull();
      if (mobileChromeGeometry.actionBar && mobileChromeGeometry.commandBar) {
        expect(mobileChromeGeometry.actionBar.bottom).toBeLessThanOrEqual(
          editorViewport.top + 0.5,
        );
        expect(mobileChromeGeometry.commandBar.bottom).toBeLessThanOrEqual(
          editorViewport.top + 0.5,
        );
      }
      await testInfo.attach("editor-mobile-geometry", {
        body: Buffer.from(
          JSON.stringify(
            { ...mobileChromeGeometry, editorGrid: editorViewport },
            null,
            2,
          ),
        ),
        contentType: "application/json",
      });
      await captureLocatorEvidence(mobileToolbar, testInfo, "editor-mobile-actionbar");
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(page.locator(".editor-action-bar-shell")).toHaveAttribute(
        "data-viewport",
        "desktop",
      );
      // A 200% browser zoom halves the CSS viewport used for responsive
      // reflow. Playwright cannot operate the browser-chrome zoom control, so
      // use the equivalent 1440x900 -> 720x450 CSS viewport. CSS `zoom: 2`
      // would only scale the already-laid-out desktop and miss breakpoints.
      await page.setViewportSize({ width: 720, height: 450 });
      expect(await page.evaluate(() => window.innerWidth)).toBe(720);
      expect(await page.evaluate(() => matchMedia("(max-width: 1024px)").matches)).toBe(true);
      await expect(page.locator(".editor-action-bar-shell")).toHaveAttribute(
        "data-viewport",
        "mobile",
      );
      await expectDocumentContainment(page);
      const zoomedToolbar = page.getByRole("toolbar", {
        name: EDITOR_CHROME_LABELS.en.toolbar,
      });
      const zoomedAttach = zoomedToolbar
        .getByRole("button", {
          name: EDITOR_CHROME_LABELS.en.attach,
          exact: true,
        });
      const horizontalPosition = await zoomedToolbar.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          viewportWidth: innerWidth,
          overflowDelta: element.scrollWidth - element.clientWidth,
        };
      });
      expect(horizontalPosition.left).toBeGreaterThanOrEqual(-2);
      expect(horizontalPosition.right).toBeLessThanOrEqual(
        horizontalPosition.viewportWidth + 2,
      );
      expect(horizontalPosition.overflowDelta).toBeLessThanOrEqual(2);
      // WCAG reflow permits vertical scrolling. Prove the primary action is
      // reachable without introducing document-level horizontal scrolling.
      await zoomedAttach.scrollIntoViewIfNeeded();
      await expect(zoomedToolbar).toBeInViewport();
      await expect(zoomedAttach).toBeInViewport();
      await zoomedAttach.focus();
      await expect(zoomedAttach).toBeFocused();
      await expectDocumentContainment(page);
      await page.setViewportSize({ width: 1440, height: 900 });
    });

    await test.step("select five files and deterministically fail B while actively cancelling D", async () => {
      await page.getByLabel("Title").fill(title);
      const editor = page.locator(".cm-content");
      await editor.click();
      await page.keyboard.press(`${await codeMirrorModifier(page)}+A`);
      await page.keyboard.insertText(`# ${title}\n\nMedia selection order follows.`);
      const attachButton = page
        .getByRole("toolbar", { name: EDITOR_CHROME_LABELS.en.toolbar })
        .getByRole("button", {
          name: EDITOR_CHROME_LABELS.en.attach,
          exact: true,
        });
      await expect(attachButton).not.toHaveAttribute("aria-disabled", "true");
      const fileChooser = page.waitForEvent("filechooser");
      await attachButton.click();
      await (await fileChooser).setFiles([...UPLOADS]);

      const queue = uploadQueue(page);
      await expect(queue.locator(".editor-upload-name")).toHaveText(
        UPLOADS.map((upload) => upload.name),
      );
      await activeCancelStarted.promise;
      const cancelledRow = uploadRow(queue, CANCELLED_NAME);
      await expect(cancelledRow.locator(".editor-upload-status")).toHaveAttribute(
        "data-status",
        "uploading",
      );
      await captureEvidence(page, testInfo, "media-batch-pending");

      const pageWrites: string[] = [];
      const contextPagesPrefix = `/api/v1/workspaces/${workspace.id}/context/pages`;
      const recordPageWrite = (request: Request) => {
        const pathname = new URL(request.url()).pathname;
        if (
          (request.method() === "POST" && pathname === contextPagesPrefix) ||
          (request.method() === "POST" &&
            new RegExp(`^${escapeRegExp(contextPagesPrefix)}/[^/]+/autosave$`).test(
              pathname,
            )) ||
          (request.method() === "PATCH" &&
            new RegExp(`^${escapeRegExp(contextPagesPrefix)}/[^/]+$`).test(pathname)) ||
          new RegExp(
            `^${escapeRegExp(contextPagesPrefix)}/[^/]+(?:/versions/[^/]+)?/publish$`,
          ).test(pathname)
        ) {
          pageWrites.push(`${request.method()} ${pathname}`);
        }
      };
      page.on("request", recordPageWrite);
      const saveButton = page.getByRole("button", {
        name: "Save draft",
        exact: true,
      });
      const publishButton = page.getByRole("button", {
        name: "Publish",
        exact: true,
      });
      await expect(saveButton).toHaveAttribute("aria-disabled", "true");
      await expect(saveButton).toHaveAttribute(
        "title",
        "Retry or remove every unresolved image upload before saving.",
      );
      await expect(publishButton).toBeDisabled();
      await saveButton.evaluate((button: HTMLButtonElement) => button.click());
      await publishButton.evaluate((button: HTMLButtonElement) => button.click());
      await expect(
        page.getByText(
          "Retry or remove every unresolved image upload before saving.",
          { exact: true },
        ),
      ).toBeVisible();
      expect(pageWrites).toEqual([]);
      page.off("request", recordPageWrite);

      await cancelledRow
        .getByRole("button", { name: `Cancel upload for ${CANCELLED_NAME}` })
        .click();
      releaseActiveCancel.resolve();
      await expect(cancelledRow.locator(".editor-upload-status")).toHaveAttribute(
        "data-status",
        "outcome-unknown",
      );
      await expect(
        uploadRow(queue, UPLOADS[4].name).locator(".editor-upload-status"),
      ).toHaveAttribute("data-status", "succeeded");
      await expect(
        uploadRow(queue, FAILED_NAME).locator(".editor-upload-status"),
      ).toHaveAttribute("data-status", "failed");
      // Snapshot preparation is concurrent, so request start order may differ
      // from selection order. The queue above and the editor slots preserve
      // user-visible order; transport evidence must prove one first attempt
      // per file without imposing an unrelated network scheduling contract.
      expect(uploadTrace).toHaveLength(UPLOADS.length);
      expect([...uploadTrace].sort()).toEqual(
        UPLOADS.map((upload) => upload.name).sort(),
      );
      await captureEvidence(page, testInfo, "media-batch-terminal");
    });

    await test.step("retry only failed work and keep the unknown cancellation save-blocking until removal", async () => {
      const queue = uploadQueue(page);
      await queue
        .getByRole("button", { name: "Retry failed", exact: true })
        .click();
      await expect(
        uploadRow(queue, FAILED_NAME).locator(".editor-upload-status"),
      ).toHaveAttribute("data-status", "succeeded");
      await expect(
        uploadRow(queue, CANCELLED_NAME).locator(".editor-upload-status"),
      ).toHaveAttribute("data-status", "outcome-unknown");
      expect(uploadAttempts.get(FAILED_NAME)).toBe(2);
      expect(uploadAttempts.get(CANCELLED_NAME)).toBe(1);
      expect(uploadTrace).toHaveLength(UPLOADS.length + 1);
      expect(uploadTrace.at(-1)).toBe(FAILED_NAME);
      for (const upload of UPLOADS) {
        expect(uploadTrace.filter((name) => name === upload.name)).toHaveLength(
          upload.name === FAILED_NAME ? 2 : 1,
        );
      }

      const saveButton = page.getByRole("button", {
        name: "Save draft",
        exact: true,
      });
      await expect(saveButton).toHaveAttribute("aria-disabled", "true");
      await uploadRow(queue, CANCELLED_NAME)
        .getByRole("button", { name: `Remove upload for ${CANCELLED_NAME}` })
        .click();
      await expect(uploadRow(queue, CANCELLED_NAME)).toHaveCount(0);
      await expect(saveButton).not.toHaveAttribute("aria-disabled", "true");
      await expect(saveButton).toBeEnabled();
      await captureEvidence(page, testInfo, "media-batch-resolved");
    });

    let created!: PagePayload;
    let canonicalBody = "";
    let assetIds: string[] = [];
    await test.step("save and publish only canonical logical references", async () => {
      // P8/P9.1: the page is no longer created by "Save draft" -- the FIRST
      // image attach already ran page-first creation (createUploadTargetSnapshot
      // in PageEditor.tsx) so every upload above had a real page/editing
      // version to target from its first attempt. By the time "Save draft" is
      // clicked here, `page` state is already populated, so this click is an
      // ordinary autosave of the caller's own open "editing" row, not a create
      // -- and there is no `media_session_id` concept to assert on any more.
      const autosaveRequest = page.waitForRequest(
        (request) =>
          new RegExp(
            `^/api/v1/workspaces/${workspace.id}/context/pages/[^/]+/autosave$`,
          ).test(new URL(request.url()).pathname) && request.method() === "POST",
      );
      const autosaveResponse = page.waitForResponse(
        (response) =>
          new RegExp(
            `^/api/v1/workspaces/${workspace.id}/context/pages/[^/]+/autosave$`,
          ).test(new URL(response.url()).pathname) &&
          response.request().method() === "POST",
      );
      await page
        .getByRole("button", { name: "Save draft", exact: true })
        .click();
      const [request, response] = await Promise.all([
        autosaveRequest,
        autosaveResponse,
      ]);
      expect(response.ok(), await response.text()).toBeTruthy();
      const savePayload = request.postDataJSON() as { body_md: string };
      const submittedBody = savePayload.body_md;
      const references = assertCanonicalBody(
        submittedBody,
        RETAINED_UPLOAD_NAMES,
      );
      assetIds = references.map((reference) => reference.assetId);
      expect(new Set(assetIds).size).toBe(RETAINED_UPLOAD_NAMES.length);

      created = (await response.json()).data as PagePayload;
      const stored = await readPage(page.request, workspace.id, created.id);
      expect(stored.body_md).toBe(created.body_md);
      assertCanonicalBody(stored.body_md, RETAINED_UPLOAD_NAMES);
      canonicalBody = stored.body_md;
      await expect(page).toHaveURL(
        new RegExp(`/workspaces/${workspace.id}/context/pages/${created.id}/edit$`),
      );

      const publishResponse = page.waitForResponse(
        (candidate) =>
          new RegExp(
            `/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/versions/${created.version_id}/publish$`,
          ).test(new URL(candidate.url()).pathname) &&
          candidate.request().method() === "POST",
      );
      await expect(
        page.getByRole("button", { name: "Publish", exact: true }),
      ).toBeEnabled();
      await page
        .getByRole("button", { name: "Publish", exact: true })
        .click();
      const publishedResponse = await publishResponse;
      expect(publishedResponse.ok()).toBeTruthy();
      const published = (await publishedResponse.json()).data as PagePayload;
      expect(published.current_published_version_id).toBe(created.version_id);
      created = published;
      await expect(page).toHaveURL(
        new RegExp(`/workspaces/${workspace.id}/context/pages/${created.id}$`),
      );
    });

    const exactVersionUrls = assetIds.map(
      (assetId) =>
        `/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/versions/${created.version_id}/media/${assetId}/content`,
    );
    const publicMediaUrls = assetIds.map(
      (assetId) =>
        `/api/v1/public/w/${workspace.slug}/pages/${created.slug}/media/${assetId}`,
    );

    await test.step("render the same retained images on private, exact-version, and public surfaces", async () => {
      const activeMedia = await listPageMedia(page.request, workspace.id, created.id);
      expect(activeMedia.map((asset) => asset.id).sort()).toEqual(
        [...assetIds].sort(),
      );
      expect(
        activeMedia.map((asset) => ({
          current: asset.current_published_reference_count,
          draft: asset.latest_draft_reference_count,
          state: asset.usage_state,
        })),
      ).toEqual(
        assetIds.map(() => ({ current: 1, draft: 0, state: "active" })),
      );
      await expectDirectStatuses(page.request, exactVersionUrls, 200);
      await expectDirectStatuses(viewerContext.request, exactVersionUrls, 200);

      const publicPage = await setPagePublic(page.request, workspace.id, created.id, true);
      expect(publicPage.body_md).toBe(canonicalBody);
      const anonymousPublicRead = await anonymousContext.request.get(
        `${backendBaseUrl}/api/v1/public/w/${workspace.slug}/pages/${created.slug}`,
      );
      expect(anonymousPublicRead.status()).toBe(200);
      const publicBody = (await anonymousPublicRead.json()).data.body_md as string;
      expect(publicBody).toBe(parsePageDocument(canonicalBody).body);
      assertCanonicalBody(publicBody, RETAINED_UPLOAD_NAMES);
      await expectDirectStatuses(
        anonymousContext.request,
        publicMediaUrls,
        200,
      );

      await page.goto(workspaceUrl(workspace.id, `context/pages/${created.id}/edit`));
      await expect(page.getByLabel("Title")).toHaveValue(title);
      await page.getByRole("tab", { name: "Reading" }).click();
      const privateGeometry = await expectMediaGeometry(
        page,
        page.locator(".preview-pane .markdown-body"),
        RETAINED_UPLOAD_NAMES,
        exactVersionUrls,
      );

      const versionPage = await page.context().newPage();
      await versionPage.goto(workspaceUrl(workspace.id, `context/pages/${created.id}`));
      await expect(
        versionPage.getByRole("heading", { name: title, exact: true }),
      ).toBeVisible();
      const versionGeometry = await expectMediaGeometry(
        versionPage,
        versionPage
          .getByRole("tabpanel", { name: /^View/ })
          .locator(".markdown-body"),
        RETAINED_UPLOAD_NAMES,
        exactVersionUrls,
      );

      const publicPageView = await anonymousContext.newPage();
      await publicPageView.goto(`/w/${workspace.slug}/pages/${created.slug}`);
      await expect(
        publicPageView.getByRole("heading", { name: title, exact: true }),
      ).toBeVisible();
      const publicGeometry = await expectMediaGeometry(
        publicPageView,
        publicPageView.locator(".public-reader .markdown-body"),
        RETAINED_UPLOAD_NAMES,
        publicMediaUrls,
      );
      await captureEvidence(
        publicPageView,
        testInfo,
        "public-reader-retained-media",
      );
      expectGeometryParity(privateGeometry, versionGeometry);
      expectGeometryParity(versionGeometry, publicGeometry);
      await Promise.all([versionPage.close(), publicPageView.close()]);
    });

    await test.step("unpublish immediately and preserve history only for authorized editors", async () => {
      const unpublished = await setPagePublic(page.request, workspace.id, created.id, false);
      expect(unpublished.current_published_version_id).toBe(created.version_id);
      const anonymousPageAfterUnpublish = await anonymousContext.request.get(
        `${backendBaseUrl}/api/v1/public/w/${workspace.slug}/pages/${created.slug}`,
      );
      expect(anonymousPageAfterUnpublish.status()).toBe(404);
      await expectDirectStatuses(
        anonymousContext.request,
        publicMediaUrls,
        404,
      );

      const nextBody = `# ${title}\n\nThe current published version no longer references media.`;
      const updateResponse = await page.request.post(
        `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages/${created.id}/autosave`,
        {
          data: {
            title,
            body_md: nextBody,
            base_version_id: created.version_id,
            base_content_hash: created.content_hash,
            expected_editing_revision: created.editing_revision,
            idempotency_key: randomUUID(),
          },
        },
      );
      expect(updateResponse.ok(), await updateResponse.text()).toBeTruthy();
      const nextDraft = (await updateResponse.json()).data as PagePayload;
      expect(nextDraft.version_id).not.toBe(created.version_id);
      const nextPublished = await publishVersion(
        page.request,
        workspace.id,
        created.id,
        nextDraft,
      );
      expect(nextPublished.current_published_version_id).toBe(
        nextDraft.version_id,
      );

      await expectDirectStatuses(
        viewerContext.request,
        exactVersionUrls,
        404,
      );
      await expectDirectStatuses(
        editorContext.request,
        exactVersionUrls,
        200,
      );
      const historyMedia = await listPageMedia(page.request, workspace.id, created.id);
      expect(historyMedia.map((asset) => asset.id).sort()).toEqual(
        [...assetIds].sort(),
      );
      for (const asset of historyMedia) {
        expect(asset.usage_state).toBe("history_only");
        expect(asset.latest_draft_reference_count).toBe(0);
        expect(asset.current_published_reference_count).toBe(0);
        expect(asset.historical_reference_count).toBeGreaterThanOrEqual(1);
        expect(asset.can_delete).toBe(false);
        const deletion = await page.request.delete(
          `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/media/assets/${asset.id}`,
          { headers: { "If-Match": asset.usage_token } },
        );
        expect(deletion.status()).toBe(409);
        expect((await deletion.json()).error_code).toBe("media_used_in_history");
      }
      await expectDirectStatuses(page.request, exactVersionUrls, 200);

      const revoked = await page.request.delete(
        `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/members/${editorId}`,
      );
      expect(revoked.ok()).toBeTruthy();
      await expectDirectStatuses(
        editorContext.request,
        exactVersionUrls,
        404,
      );

      const members = await page.request.get(
        `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/members`,
      );
      expect(members.ok()).toBeTruthy();
      const memberItems = (await members.json()).data.items as Array<{
        user_id: string;
      }>;
      expect(memberItems.some((member) => member.user_id === editorId)).toBe(
        false,
      );
      expect(memberItems.some((member) => member.user_id === viewerId)).toBe(
        true,
      );
    });
  } finally {
    releaseActiveCancel.resolve();
    await page.unroute(SESSION_UPLOAD_ROUTE).catch(() => undefined);
    await Promise.all([
      viewerContext.close(),
      editorContext.close(),
      anonymousContext.close(),
    ]);
  }
});

async function expectEditorChrome(
  page: Page,
  labels: (typeof EDITOR_CHROME_LABELS)[keyof typeof EDITOR_CHROME_LABELS],
) {
  const toolbar = page.getByRole("toolbar", { name: labels.toolbar });
  const attach = toolbar.getByRole("button", {
    name: labels.attach,
    exact: true,
  });
  const more = toolbar.getByRole("button", { name: labels.more });
  await expect(toolbar).toBeVisible();
  await expect(attach).toBeVisible();

  const backingInput = page.locator(
    '.editor-command-bar input[type="file"][multiple]',
  );
  await expect(backingInput).toHaveCount(1);
  await expect(backingInput).toHaveAttribute("aria-hidden", "true");
  await expect(backingInput).toHaveAttribute("tabindex", "-1");

  const rovingButtons = toolbar.locator('button[tabindex="0"]');
  await expect(rovingButtons).toHaveCount(1);
  await rovingButtons.focus();
  await page.keyboard.press("End");
  await expect(more).toBeFocused();
  await page.keyboard.press("Enter");
  const moreMenu = page.getByRole("menu", { name: labels.more });
  await expect(moreMenu).toBeVisible();
  await expect(moreMenu.getByRole("menuitem").first()).toBeFocused();
  const actionBarCommandIds = [
    ...(await commandIds(toolbar)),
    ...(await commandIds(moreMenu)),
  ];
  expect([...new Set(actionBarCommandIds)].sort()).toEqual(
    ACTION_BAR_COMMAND_IDS,
  );
  await page.keyboard.press("Escape");
  await expect(moreMenu).toHaveCount(0);
  await expect(more).toBeFocused();

  const sourceTab = page.getByRole("tab", { name: labels.source, exact: true });
  const readingTab = page.getByRole("tab", {
    name: labels.reading,
    exact: true,
  });
  await sourceTab.click();
  await sourceTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(readingTab).toHaveAttribute("aria-selected", "true");
  await expect(readingTab).toBeFocused();
  const readingDisabledBold = toolbar.locator('[data-command-id="bold"]');
  await expect(readingDisabledBold).toHaveAttribute("aria-disabled", "true");
  const disabledDescriptionId = await readingDisabledBold.getAttribute(
    "aria-describedby",
  );
  expect(disabledDescriptionId).toBeTruthy();
  await expect(page.locator(`#${disabledDescriptionId}`)).not.toBeEmpty();
  await page.keyboard.press("ArrowLeft");
  await expect(sourceTab).toHaveAttribute("aria-selected", "true");
  await expect(sourceTab).toBeFocused();

  const editor = page.locator(".cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+/");
  const slashDialog = page.getByRole("dialog", { name: labels.insertBlock });
  const slashCombobox = slashDialog.getByRole("combobox", {
    name: labels.filterBlocks,
  });
  const slashListbox = slashDialog.getByRole("listbox");
  await expect(slashDialog).toBeVisible();
  await expect(slashCombobox).toBeFocused();
  const listboxId = await slashListbox.getAttribute("id");
  expect(listboxId).toBeTruthy();
  await expect(slashCombobox).toHaveAttribute("aria-controls", listboxId!);
  const activeOptionId = await slashCombobox.getAttribute(
    "aria-activedescendant",
  );
  expect(activeOptionId).toBeTruthy();
  await expect(page.locator(`#${activeOptionId}`)).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect([...new Set(await commandIds(slashListbox))].sort()).toEqual(
    SLASH_COMMAND_IDS,
  );
  await page.keyboard.press("ArrowDown");
  await expect(slashCombobox).not.toHaveAttribute(
    "aria-activedescendant",
    activeOptionId!,
  );
  await page.keyboard.press("Escape");
  await expect(slashDialog).toHaveCount(0);
  await expect(editor).toBeFocused();
}

async function commandIds(root: Locator) {
  return root.locator("[data-command-id]").evaluateAll((elements) =>
    elements.flatMap((element) => {
      const commandId = element.getAttribute("data-command-id");
      return commandId ? [commandId] : [];
    }),
  );
}

async function expectDocumentContainment(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(2);
}

function uploadQueue(page: Page) {
  return page.getByRole("region", { name: "Image upload queue" });
}

function uploadRow(queue: Locator, filename: string) {
  return queue.getByRole("listitem").filter({ hasText: filename });
}

function multipartFilename(request: Request) {
  const body = request.postDataBuffer();
  if (!body) return null;
  return /filename="([^"]+)"/.exec(body.toString("latin1"))?.[1] ?? null;
}

async function currentUserId(request: APIRequestContext) {
  const response = await request.get(`${backendBaseUrl}/api/v1/auth/me`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()).data.user_id as string;
}

async function addWorkspaceMember(
  request: APIRequestContext,
  workspaceId: string,
  email: string,
  role: "viewer" | "editor",
) {
  const response = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/members`,
    {
      data: {
        email,
        role,
        confirm_first_share: true,
      },
    },
  );
  expect([200, 201]).toContain(response.status());
}

async function makeWorkspacePublic(
  request: APIRequestContext,
  workspaceId: string,
) {
  const currentResponse = await request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/access`,
  );
  expect(currentResponse.ok()).toBeTruthy();
  const current = (await currentResponse.json()).data as {
    visibility: "private" | "team" | "public";
    join_policy: "invite_only" | "request";
    updated_at: string;
    pending_join_request_count: number;
    public_page_count: number;
  };
  if (current.visibility === "public") return;
  const response = await request.patch(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/access`,
    {
      data: {
        visibility: "public",
        join_policy: current.join_policy,
        expected_updated_at: current.updated_at,
        expected_pending_join_request_count:
          current.pending_join_request_count,
        expected_public_page_count: current.public_page_count,
        confirm_access_change: true,
      },
    },
  );
  expect(response.ok()).toBeTruthy();
}

async function readPage(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
) {
  const response = await request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${pageId}`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).data as PagePayload;
}

async function setPagePublic(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
  isPublic: boolean,
) {
  const response = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/publish`,
    { data: { is_public: isPublic } },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).data as PagePayload;
}

async function publishVersion(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
  version: PagePayload,
) {
  const response = await request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/versions/${version.version_id}/publish`,
    {
      data: {
        reason: "E2E retained Context media history",
        publish_despite_findings: false,
        expected_content_hash: version.content_hash,
      },
    },
  );
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).data as PagePayload;
}

async function listPageMedia(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
) {
  const response = await request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages/${pageId}/media?usage=used`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).data as ContextMediaAsset[];
}

async function expectDirectStatuses(
  request: APIRequestContext,
  relativeUrls: string[],
  status: number,
) {
  for (const relativeUrl of relativeUrls) {
    const response = await request.get(`${backendBaseUrl}${relativeUrl}`);
    expect(response.status(), relativeUrl).toBe(status);
  }
}

async function codeMirrorModifier(page: Page): Promise<"Meta" | "Control"> {
  return page.evaluate(() =>
    /Mac|iPhone|iPad|iPod/i.test(navigator.platform) ? "Meta" : "Control",
  );
}

async function captureEvidence(
  page: Page,
  testInfo: TestInfo,
  name: string,
) {
  await evidenceScreenshot(page, testInfo, name, { fullPage: false });
}

async function captureLocatorEvidence(
  locator: Locator,
  testInfo: TestInfo,
  name: string,
) {
  await evidenceLocatorScreenshot(locator, testInfo, name);
}

function assertCanonicalBody(
  body: string,
  expectedNames: readonly string[],
) {
  expect(body).not.toContain("\u200b");
  expect(body).not.toMatch(/<!--\s*upload\s+(?:failed|cancelled)/i);
  expect(body).not.toMatch(/(?:data|blob):/i);
  expect(body).not.toContain("/api/v1/");
  expect(body).not.toMatch(/https?:\/\/[^\s)]+/i);
  const references = [...body.matchAll(new RegExp(
    `!\\[([^\\]]+)\\]\\(dtx-media:\\/\\/asset\\/(${UUID_PATTERN})\\)`,
    "gi",
  ))].map((match) => ({ alt: match[1], assetId: match[2] }));
  expect(references.map((reference) => reference.alt)).toEqual(expectedNames);
  return references;
}

async function expectMediaGeometry(
  page: Page,
  root: Locator,
  expectedNames: readonly string[],
  expectedPaths: string[],
) {
  const images = root.locator("img.markdown-image");
  await expect(images).toHaveCount(expectedNames.length);
  await expect
    .poll(() =>
      images.evaluateAll((elements) =>
        elements.every((element) => {
          const image = element as HTMLImageElement;
          return image.complete && image.naturalWidth > 0 && image.naturalHeight > 0;
        }),
      ),
    )
    .toBe(true);
  const samples = await images.evaluateAll((elements) =>
    elements.map((element) => {
      const image = element as HTMLImageElement;
      const container = image.closest<HTMLElement>(".markdown-body");
      const rect = image.getBoundingClientRect();
      return {
        alt: image.alt,
        containerWidth: container?.getBoundingClientRect().width ?? 0,
        height: rect.height,
        naturalHeight: image.naturalHeight,
        naturalWidth: image.naturalWidth,
        path: new URL(image.getAttribute("src") ?? "", window.location.href)
          .pathname,
        width: rect.width,
      };
    }),
  );
  expect(samples.map((sample) => sample.alt)).toEqual(expectedNames);
  expect(samples.map((sample) => sample.path)).toEqual(expectedPaths);
  for (const sample of samples) {
    expect(sample.width).toBeGreaterThan(0);
    expect(sample.height).toBeGreaterThan(0);
    expect(sample.width).toBeLessThanOrEqual(sample.containerWidth + 1);
    expect(sample.width).toBeLessThanOrEqual(sample.naturalWidth + 1);
    expect(sample.width / sample.height).toBeCloseTo(
      sample.naturalWidth / sample.naturalHeight,
      2,
    );
  }
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      ),
    )
    .toBeLessThanOrEqual(2);
  return samples as GeometrySample[];
}

function expectGeometryParity(
  left: GeometrySample[],
  right: GeometrySample[],
) {
  expect(right.map((sample) => sample.alt)).toEqual(
    left.map((sample) => sample.alt),
  );
  for (let index = 0; index < left.length; index += 1) {
    expect(right[index].naturalWidth).toBe(left[index].naturalWidth);
    expect(right[index].naturalHeight).toBe(left[index].naturalHeight);
    expect(right[index].width).toBeCloseTo(left[index].width, 1);
    expect(right[index].height).toBeCloseTo(left[index].height, 1);
  }
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function webpAnimationFrameCount(content: Buffer) {
  if (
    content.length < 12 ||
    content.toString("ascii", 0, 4) !== "RIFF" ||
    content.toString("ascii", 8, 12) !== "WEBP" ||
    content.readUInt32LE(4) + 8 !== content.length
  ) {
    throw new Error("Animated WebP fixture must be an exact RIFF/WEBP container.");
  }
  let frames = 0;
  let offset = 12;
  while (offset < content.length) {
    if (offset + 8 > content.length) {
      throw new Error("Animated WebP fixture has a truncated chunk header.");
    }
    const chunkType = content.toString("ascii", offset, offset + 4);
    const chunkBytes = content.readUInt32LE(offset + 4);
    const nextOffset = offset + 8 + chunkBytes + (chunkBytes % 2);
    if (nextOffset > content.length) {
      throw new Error("Animated WebP fixture has a truncated chunk payload.");
    }
    if (chunkType === "ANMF") {
      frames += 1;
    }
    offset = nextOffset;
  }
  return frames;
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
