import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  expect,
  isNetworkChangeConsoleEcho,
  isTransientNetworkImportError,
  test,
  type APIRequestContext,
  type APIResponse,
  type BoundingBox,
  type Page,
  type TestInfo,
} from "./fixtures";

import {
  authenticatePage,
  backendBaseUrl,
  currentWorkspace,
} from "./auth";
import { evidenceScreenshot } from "./evidence";
import { redactFailedRequest } from "./redact-network";

type SeededPage = {
  content_hash: string;
  id: string;
  slug: string;
  version_id: string;
};

type SeededTreeNode = {
  id: string;
  parent_id: string | null;
  title: string;
};

type BrowserFailure = {
  detail: string;
  kind: "console" | "pageerror" | "requestfailed" | "response";
  url?: string;
};

type BrowserFailureMonitor = {
  armExpectedPreviewTeardown: (workspaceId: string) => () => void;
  failures: BrowserFailure[];
};

test.describe("2026-08-09 UI fix acceptance", () => {
  test("Context blank-reason Move remains usable", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    const failures = monitorBrowserFailures(page);
    await setEnglish(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await authenticatePage(page, testInfo, "ui-fix-context");
    const workspace = await currentWorkspace(page.request);
    const suffix = fixtureSuffix(testInfo);

    await test.step("Move accepts an empty Reason and keeps the picker compact", async () => {
      const sourceParent = await createFolder(
        page.request,
        workspace.id,
        null,
        `move-source-parent-${suffix}`,
        `Move source parent ${suffix}`,
      );
      const sourceNode = await createFolder(
        page.request,
        workspace.id,
        sourceParent.id,
        `move-source-${suffix}`,
        `Move source ${suffix}`,
      );
      const targetParent = await createFolder(
        page.request,
        workspace.id,
        null,
        `move-target-${suffix}`,
        `Move target ${suffix}`,
      );
      await createFolder(
        page.request,
        workspace.id,
        targetParent.id,
        `move-target-child-${suffix}`,
        `Move target child ${suffix}`,
      );

      await page.goto("/context");
      const sourceParentRow = page.getByRole("treeitem", {
        exact: true,
        name: sourceParent.title,
      });
      await expect(sourceParentRow).toBeVisible();
      await sourceParentRow.getByRole("button", { name: `Expand ${sourceParent.title}` }).click();
      const sourceRow = page.getByRole("treeitem", {
        exact: true,
        name: sourceNode.title,
      });
      await sourceRow.hover();
      const sourceActionButton = sourceRow.getByRole("button", {
        name: `Open actions for ${sourceNode.title}`,
      });
      // Headless Firefox reports no fine hover capability, so exercise the
      // keyboard-accessible focus path that reveals the same action surface.
      await sourceActionButton.focus();
      await sourceActionButton.click();
      await page.getByRole("menuitem", { name: "Move", exact: true }).click();

      const dialog = page.getByRole("dialog", {
        name: `Move ${sourceNode.title}`,
      });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("textbox", { name: "Reason (optional)" })).toHaveValue("");
      const close = dialog.getByRole("button", { name: "Close dialog" });
      await expect(close).toBeVisible();
      await expect(close.locator("svg")).toHaveCount(1);
      expect((await close.innerText()).trim()).toBe("");

      await dialog.getByRole("button", { name: "Target parent" }).click();
      const pickerTree = page.getByRole("tree", { name: "Target parent tree" });
      // The row's accessible name also includes its named expand/collapse
      // button. Identify the fixture node independently of that changing label.
      const targetRow = pickerTree.getByRole("treeitem").filter({
        has: page.locator(`[data-tree-toggle="${targetParent.id}"]`),
      });
      await expect(targetRow).toBeVisible();
      const chevron = targetRow.locator(".tree-picker-chevron");
      const pickerGeometry = await targetRow.evaluate((row) => {
        const toRect = (rect: DOMRect) => ({
          height: rect.height,
          width: rect.width,
          x: rect.x,
          y: rect.y,
        });
        const chevronElement = row.querySelector<HTMLElement>(".tree-picker-chevron");
        const label = row.querySelector<HTMLElement>(".tree-picker-label");
        if (!chevronElement || !label) {
          throw new Error("target picker row is missing its chevron or label");
        }
        const rowBox = row.getBoundingClientRect();
        const chevronBox = chevronElement.getBoundingClientRect();
        const labelBox = label.getBoundingClientRect();
        const hitSlop = getComputedStyle(chevronElement, "::before");
        const labelPoint = document.elementFromPoint(
          labelBox.left + Math.min(4, labelBox.width / 2),
          labelBox.top + labelBox.height / 2,
        );
        return {
          chevron: toRect(chevronBox),
          hitAtLabelClass: labelPoint instanceof Element ? labelPoint.className : null,
          hitSlopHeight: hitSlop.height,
          hitSlopWidth: hitSlop.width,
          label: toRect(labelBox),
          labelGapFromChevron: labelBox.left - chevronBox.right,
          row: toRect(rowBox),
        };
      });
      expect(pickerGeometry.chevron.width).toBe(22);
      expect(pickerGeometry.chevron.height).toBe(22);
      expect(pickerGeometry.hitSlopWidth).toBe("44px");
      expect(pickerGeometry.hitSlopHeight).toBe("44px");
      expect(pickerGeometry.row.height).toBeGreaterThanOrEqual(44);
      expect(pickerGeometry.labelGapFromChevron).toBeLessThanOrEqual(26);
      expect(String(pickerGeometry.hitAtLabelClass)).not.toContain("tree-picker-chevron");
      await saveEvidence(page, testInfo, "context-move-dialog-blank-reason", {
        pickerGeometry,
        reason: await dialog
          .getByRole("textbox", { name: "Reason (optional)" })
          .inputValue(),
        url: redactedUrl(page.url()),
      });

      await chevron.click();
      await targetRow.click();
      const moveRequestPromise = page.waitForRequest((request) =>
        request.url() === `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/tree/move` &&
        request.method() === "POST",
      );
      const moveResponsePromise = page.waitForResponse((response) =>
        response.url() === `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/tree/move` &&
        response.request().method() === "POST",
      );
      await dialog.getByRole("button", { name: "Move", exact: true }).click();
      const moveRequest = await moveRequestPromise;
      const moveBody = moveRequest.postDataJSON() as Record<string, unknown>;
      expect(moveBody.node_id).toBe(sourceNode.id);
      expect(moveBody.new_parent_id).toBe(targetParent.id);
      expect(moveBody).not.toHaveProperty("reason");
      expect((await moveResponsePromise).ok()).toBeTruthy();
      await expect(dialog).toBeHidden();
      await saveEvidence(page, testInfo, "context-move-blank-reason", {
        moveRequestBody: moveBody,
        pickerGeometry,
        url: redactedUrl(page.url()),
      });
    });

    await assertBrowserFailuresClean(failures, testInfo, "context-browser-failures");
  });

  test("F10 Context destination expansion keeps the owning properties popover open", async ({
    browser,
    browserName,
    page,
  }, testInfo) => {
    test.setTimeout(180_000);
    const desktopFailures = monitorBrowserFailures(page);
    await setEnglish(page);
    await authenticatePage(page, testInfo, "f10-context-location-desktop");
    const desktopWorkspace = await currentWorkspace(page.request);

    await verifyF10ContextLocationPicker(page, testInfo, {
      input: "pointer",
      suffix: `${fixtureSuffix(testInfo)}-desktop`,
      viewport: { width: 1440, height: 900 },
      workspaceId: desktopWorkspace.id,
    });
    await assertBrowserFailuresClean(
      desktopFailures,
      testInfo,
      "f10-context-location-desktop-browser-failures",
    );

    const mobileContext = await browser.newContext({
      hasTouch: true,
      // Firefox supports touch input, but not mobile viewport emulation.
      isMobile: browserName !== "firefox",
      viewport: { width: 390, height: 844 },
    });
    try {
      const mobilePage = await mobileContext.newPage();
      const mobileFailures = monitorBrowserFailures(mobilePage);
      await setEnglish(mobilePage);
      await authenticatePage(mobilePage, testInfo, "f10-context-location-touch");
      const mobileWorkspace = await currentWorkspace(mobilePage.request);

      await verifyF10ContextLocationPicker(mobilePage, testInfo, {
        input: "touch",
        suffix: `${fixtureSuffix(testInfo)}-touch`,
        viewport: { width: 390, height: 844 },
        workspaceId: mobileWorkspace.id,
      });
      await assertBrowserFailuresClean(
        mobileFailures,
        testInfo,
        "f10-context-location-touch-browser-failures",
      );
    } finally {
      await mobileContext.close();
    }
  });

  test("Editor properties, More menu, and Split layout match the fixed behavior", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    const failures = monitorBrowserFailures(page);
    await setEnglish(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await authenticatePage(page, testInfo, "ui-fix-editor");
    const workspace = await currentWorkspace(page.request);
    const suffix = fixtureSuffix(testInfo);
    const seededPage = await createContextPage(
      page.request,
      workspace.id,
      `UI fix editor ${suffix}`,
      undefined,
      "# UI fix editor\n\n1. First numbered item\n\nA paragraph for preview geometry.",
    );

    await page.goto(`/context/${seededPage.slug}/edit`);
    await expect(page.locator(".cm-content")).toBeVisible();

    await test.step("Page properties puts Aliases under Advanced and removes Cover URL", async () => {
      await page.getByRole("button", { name: "Page properties" }).click();
      const properties = page.getByRole("region", { name: "Page properties" });
      await expect(properties).toBeVisible();
      await expect(properties.getByLabel("Aliases")).toHaveCount(0);
      await expect(properties.getByText("Cover URL", { exact: true })).toHaveCount(0);
      await expect(properties.getByLabel("Cover URL")).toHaveCount(0);
      const advanced = properties.getByRole("button", { name: /^Advanced settings/ });
      await expect(advanced).toHaveAttribute("aria-expanded", "false");
      await advanced.click();
      await expect(advanced).toHaveAttribute("aria-expanded", "true");
      await expect(properties.getByLabel("Aliases")).toBeVisible();
      await expect(properties.getByLabel("Slug")).toBeVisible();
      await saveEvidence(page, testInfo, "editor-page-properties-advanced", {
        geometry: await surfaceGeometry(page, [
          ".editor-properties-popover",
          ".advanced-disclosure",
          "#editor-aliases",
        ]),
        url: redactedUrl(page.url()),
      });
      await properties.getByRole("button", { name: "Close properties" }).click();
    });

    await test.step("Action glyphs and More menu expose readable labels and focus state", async () => {
      const toolbar = page.getByRole("toolbar", { name: "Editor actions" });
      for (const command of [
        "Heading",
        "Bold",
        "Italic",
        "Strikethrough",
        "Inline code",
        "Link",
        "Image",
        "Attach files",
        "Bulleted list",
        "Task list",
        "Quote",
        "Table",
      ]) {
        await expect(toolbar.getByRole("button", { name: command, exact: true })).toBeVisible();
      }
      for (const command of [
        "Inline code",
        "Link",
        "Image",
        "Attach files",
        "Bulleted list",
        "Task list",
        "Quote",
        "Table",
      ]) {
        await expect(
          toolbar.getByRole("button", { name: command, exact: true }).locator("svg"),
        ).toHaveCount(1);
      }

      await toolbar.getByRole("button", { name: "More editor actions" }).click();
      const menu = page.getByRole("menu", { name: "More editor actions" });
      await expect(menu).toBeVisible();
      for (const label of [
        "Numbered list",
        "Code block",
        "Wikilink",
        "Citation",
        "Emoji",
        "Math",
        "Comment",
      ]) {
        await expect(menu.getByRole("menuitem", { name: label, exact: true })).toBeVisible();
      }
      // Mouse opening deliberately does not draw a focus-visible ring. Move
      // through the roving menu with the keyboard before checking the ring.
      await page.keyboard.press("ArrowDown");
      const focused = menu.getByRole("menuitem", { name: "Code block", exact: true });
      await expect(focused).toBeFocused();
      const focusState = await focused.evaluate((element) => {
        const toRect = (rect: DOMRect) => ({
          height: rect.height,
          width: rect.width,
          x: rect.x,
          y: rect.y,
        });
        const elementBox = element.getBoundingClientRect();
        const menuBox = element.parentElement?.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          box: toRect(elementBox),
          boxShadow: style.boxShadow,
          menu: menuBox ? toRect(menuBox) : null,
          outlineStyle: style.outlineStyle,
        };
      });
      expect(focusState.menu).not.toBeNull();
      expect(focusState.box.x).toBeGreaterThanOrEqual(focusState.menu!.x);
      expect(focusState.box.x + focusState.box.width).toBeLessThanOrEqual(
        focusState.menu!.x + focusState.menu!.width,
      );
      expect(focusState.boxShadow).toContain("inset");
      await saveEvidence(page, testInfo, "editor-more-menu", {
        focusState,
        geometry: await surfaceGeometry(page, [
          ".editor-action-more-menu",
          '.editor-action-more-menu [data-command-id="code-block"]',
        ]),
        url: redactedUrl(page.url()),
      });
      await page.keyboard.press("Escape");
      await expect(menu).toBeHidden();
    });

    await test.step("Split geometry is bounded at 1440 and 1024, and unavailable at 390", async () => {
      await page.getByRole("tab", { name: "Split", exact: true }).click();
      const desktopGeometry = await expectSplitGeometry(page, "vertical");
      await saveEvidence(page, testInfo, "editor-split-1440x900", {
        geometry: desktopGeometry,
        viewport: { height: 900, width: 1440 },
      });

      await page.setViewportSize({ width: 1024, height: 768 });
      await expect(page.locator('.editor-grid[data-layout="split"]')).toBeVisible();
      const tabletGeometry = await expectSplitGeometry(page, "vertical");
      await saveEvidence(page, testInfo, "editor-split-1024x768", {
        geometry: tabletGeometry,
        viewport: { height: 768, width: 1024 },
      });

      await page.setViewportSize({ width: 390, height: 844 });
      await expect(page.getByRole("tab", { name: "Split", exact: true })).toHaveCount(0);
      await expect(page.locator('.editor-grid[data-layout="source"]')).toBeVisible();
      await expect(page.locator(".source-pane")).toBeVisible();
      await expect(page.locator(".preview-pane")).toHaveCount(0);
      await expect(page.locator(".editor-split-separator")).toHaveCount(0);
      await expectNoHorizontalOverflow(page, "390px editor");
      await saveEvidence(page, testInfo, "editor-source-390x844", {
        geometry: await surfaceGeometry(page, [
          ".editor-panel",
          ".editor-grid",
          ".source-pane",
        ]),
        viewport: { height: 844, width: 390 },
      });
    });

    await assertBrowserFailuresClean(failures, testInfo, "editor-browser-failures");
  });

  test("Review header and Library detail actions keep the cleaned-up chrome", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120_000);
    const failures = monitorBrowserFailures(page);
    await setEnglish(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await authenticatePage(page, testInfo, "ui-fix-review-library");
    const workspace = await currentWorkspace(page.request);

    await test.step("Review rail header no longer repeats Preview", async () => {
      await page.goto("/review");
      const header = page.locator(".review-rail-header");
      await expect(header).toBeVisible();
      await expect(header.getByText("Preview", { exact: true })).toHaveCount(0);
      await expect(header).not.toContainText("Preview");
      await saveEvidence(page, testInfo, "review-rail-header", {
        geometry: await surfaceGeometry(page, [
          ".review-rail-header",
          ".review-rail-heading",
          ".review-rail-actions",
        ]),
        text: (await header.innerText()).trim(),
        url: redactedUrl(page.url()),
      });
    });

    await test.step("Library detail uses an 8px action gap and one mobile Upload", async () => {
      await page.goto("/library");
      await expect(
        page.getByRole("heading", { name: "Library", exact: true }),
      ).toBeVisible();
      await page.waitForLoadState("networkidle");
      const uploadEndpoint =
        `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/library/items`;
      const uploadedResponse = page.waitForResponse((response) =>
        response.url() === uploadEndpoint &&
        response.request().method() === "POST" &&
        response.ok(),
      );
      // Upload selection can replace PreviewPane while its authenticated
      // manifest/content request is still in flight. Arm one narrowly scoped,
      // one-shot allowance for that teardown window; every other abort remains
      // a browser failure.
      const disarmExpectedPreviewTeardown = failures.armExpectedPreviewTeardown(
        workspace.id,
      );
      await page.getByLabel("Upload Library file").setInputFiles({
        buffer: Buffer.from('{"uiFix":true}', "utf8"),
        mimeType: "application/json",
        name: `ui-fix-actions-${fixtureSuffix(testInfo)}.json`,
      });
      const uploaded = await responseData<{ id: string }>(
        await uploadedResponse,
        "upload Library UI fixture",
      );
      await expect(page).toHaveURL(
        new RegExp(`(?:\\?|&)target=${uploaded.id}(?:&|$)`),
      );
      const actions = page.locator(".library-reader-actions");
      await expect(actions).toBeVisible();
      // Upload was deliberately removed from the per-item action bar: uploading
      // creates a new Library item, so it belongs to the screen, not to the item
      // being read. The bar now carries per-item actions only (download the
      // original, delete, and Edit Markdown when the version is editable).
      // Asserting absence keeps it from drifting back in.
      await expect(actions.locator(".library-upload-action")).toHaveCount(0);
      await expect(actions.locator(".library-reader-download-action")).toBeVisible();
      await expect(actions.locator(".library-delete-action")).toBeVisible();
      const desktopActionMetrics = await actions.evaluate((element) => {
        const toRect = (rect: DOMRect) => ({
          height: rect.height,
          width: rect.width,
          x: rect.x,
          y: rect.y,
        });
        const style = getComputedStyle(element);
        const download = element.querySelector<HTMLElement>(
          ".library-reader-download-action",
        );
        const deleteAction = element.querySelector<HTMLElement>(".library-delete-action");
        return {
          actionGroup: toRect(element.getBoundingClientRect()),
          deleteAction: deleteAction ? toRect(deleteAction.getBoundingClientRect()) : null,
          download: download ? toRect(download.getBoundingClientRect()) : null,
          gap: style.gap,
        };
      });
      expect(desktopActionMetrics.gap).toBe("8px");
      await saveEvidence(page, testInfo, "library-actions-1440x900", {
        geometry: desktopActionMetrics,
        url: redactedUrl(page.url()),
      });

      await page.setViewportSize({ width: 390, height: 844 });
      await expect(actions.locator(".library-upload-action")).toHaveCount(0);
      await expect(actions.locator(".library-delete-action")).toBeVisible();
      // Upload lives in the Library screen header now; the per-item bar and the
      // mobile FAB both dropped theirs. The invariant this step protects is
      // unchanged — exactly one *visible* Upload affordance at 390px, never a
      // duplicate — so count visible buttons page-wide rather than inside the
      // FAB slot. (The visually-hidden file input also maps to the button role,
      // hence filtering on visibility rather than asserting a raw count.)
      const visibleUploads = page
        .getByRole("button", { name: /^Upload/ })
        .filter({ visible: true });
      await expect(visibleUploads).toHaveCount(1);
      await expectNoHorizontalOverflow(page, "390px Library detail");
      await saveEvidence(page, testInfo, "library-actions-390x844", {
        geometry: await surfaceGeometry(page, [
          ".library-reader-actions",
          ".library-delete-action",
          ".workspace-mobile-actions",
        ]),
        visibleUploadCount: await visibleUploads.count(),
        url: redactedUrl(page.url()),
      });
      disarmExpectedPreviewTeardown();
    });

    await assertBrowserFailuresClean(failures, testInfo, "review-library-browser-failures");
  });
});

async function createContextPage(
  request: APIRequestContext,
  workspaceId: string,
  title: string,
  placementParentNodeId?: string,
  bodyMd = "# UI fix acceptance\n\nIsolated Playwright fixture.",
) {
  return responseData<SeededPage>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`,
      {
        data: {
          body_md: bodyMd,
          page_type: "guide",
          ...(placementParentNodeId
            ? { placement_parent_node_id: placementParentNodeId }
            : {}),
          title,
        },
      },
    ),
    `create Context page ${title}`,
  );
}

async function createFolder(
  request: APIRequestContext,
  workspaceId: string,
  parentId: string | null,
  slug: string,
  title: string,
) {
  return responseData<SeededTreeNode>(
    await request.post(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/tree/nodes`,
      {
        data: {
          node_kind: "folder",
          parent_id: parentId,
          section: "context",
          slug,
          title,
        },
      },
    ),
    `create Context folder ${title}`,
  );
}

async function verifyF10ContextLocationPicker(
  page: Page,
  testInfo: TestInfo,
  options: {
    input: "pointer" | "touch";
    suffix: string;
    viewport: { height: number; width: number };
    workspaceId: string;
  },
) {
  const { input, suffix, viewport, workspaceId } = options;
  const titlePrefix = `F10 location ${suffix}`;
  const parent = await createFolder(
    page.request,
    workspaceId,
    null,
    `f10-location-parent-${suffix}`,
    `${titlePrefix} parent`,
  );
  const child = await createFolder(
    page.request,
    workspaceId,
    parent.id,
    `f10-location-child-${suffix}`,
    `${titlePrefix} child`,
  );
  await createFolder(
    page.request,
    workspaceId,
    child.id,
    `f10-location-grandchild-${suffix}`,
    `${titlePrefix} grandchild`,
  );

  await page.setViewportSize(viewport);
  await page.goto(`${backendBaseUrl}/context/new`, { waitUntil: "networkidle" });
  const propertiesTrigger = page.getByRole("button", {
    name: "Page properties",
    exact: true,
  });
  await propertiesTrigger.click();
  const properties = page.getByRole("region", { name: "Page properties" });
  await expect(properties).toBeVisible();
  const destination = properties.getByRole("button", {
    name: "Destination",
    exact: true,
  });
  await expect(destination).toBeEnabled();
  await destination.click();
  const tree = page.getByRole("tree", { name: "Destination tree" });
  await expect(tree).toBeVisible();
  const search = page.getByRole("searchbox", { name: "Search target parents" });
  await expect(search).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(tree).toHaveCount(0);
  await expect(properties).toBeVisible();
  await expect(destination).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(properties).toHaveCount(0);
  await expect(propertiesTrigger).toBeFocused();
  await propertiesTrigger.click();
  await destination.click();
  await expect(tree).toBeVisible();
  await expect(search).toBeFocused();

  const expandParent = tree.getByRole("button", {
    name: `Expand ${parent.title}`,
    exact: true,
  });
  // The selected Context root begins collapsed. Use the shipped keyboard path
  // to reveal the independent fixture branch before testing its chevron.
  if (await expandParent.count() === 0) {
    await tree.focus();
    await tree.press("ArrowRight");
  }
  await expect(expandParent).toBeVisible();
  const parentRow = tree
    .locator(`[role="treeitem"]:has([data-tree-toggle="${parent.id}"])`)
    .first();
  const childLabel = tree.getByText(child.title, { exact: true });
  await expect(parentRow).toHaveAttribute("aria-expanded", "false");
  await expect(childLabel).toHaveCount(0);
  const originalDestination = (await destination.innerText()).trim();
  const viewportKey = `${viewport.width}x${viewport.height}`;

  await saveEvidence(page, testInfo, `f10-context-location-before-${viewportKey}`, {
    destination: originalDestination,
    input,
    parentExpanded: await parentRow.getAttribute("aria-expanded"),
    popupVisible: await properties.isVisible(),
    treeVisible: await tree.isVisible(),
    viewport,
  });

  const writes: string[] = [];
  const recordPageWrite = (request: Request) => {
    const pathname = new URL(request.url()).pathname;
    if (
      ["PATCH", "POST", "PUT"].includes(request.method())
      // P9.1: the top-level /api/v1/pages* family is deleted; Context
      // page writes go to the workspace-scoped route now.
      && pathname.startsWith(
        `/api/v1/workspaces/${workspaceId}/context/pages`,
      )
    ) {
      writes.push(`${request.method()} ${pathname}`);
    }
  };
  page.on("request", recordPageWrite);
  try {
    if (input === "touch") {
      const box = requiredBox(
        await expandParent.boundingBox(),
        `F10 ${viewportKey} parent expand control`,
      );
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
    } else {
      await expandParent.click();
    }
    await expect(parentRow).toHaveAttribute("aria-expanded", "true");
    await expect(childLabel).toBeVisible();
    expect(writes, "expanding a destination must not write or select it").toEqual([]);
  } finally {
    page.off("request", recordPageWrite);
  }
  await expect(properties).toBeVisible();
  await expect(tree).toBeVisible();
  expect((await destination.innerText()).trim()).toBe(originalDestination);
  if (input === "pointer") {
    // Do not focus the tree first: the native chevron owns mouse focus.
    await expect(parentRow.getByRole("button", {
      name: `Collapse ${parent.title}`, exact: true,
    })).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(parentRow).toHaveAttribute("aria-expanded", "false");
    await expect(tree).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(parentRow).toHaveAttribute("aria-expanded", "true");
    await expect(childLabel).toBeVisible();
    expect((await destination.innerText()).trim()).toBe(originalDestination);
  }
  await saveEvidence(
    page,
    testInfo,
    `f10-context-location-${input === "touch" ? "touch" : "chevron"}-${viewportKey}`,
    {
      childVisible: await childLabel.isVisible(),
      destination: (await destination.innerText()).trim(),
      input,
      parentExpanded: await parentRow.getAttribute("aria-expanded"),
      popupVisible: await properties.isVisible(),
      treeVisible: await tree.isVisible(),
      viewport,
    },
  );

  await tree.getByRole("button", { name: `Collapse ${parent.title}`, exact: true }).click();
  await expect(parentRow).toHaveAttribute("aria-expanded", "false");
  await parentRow.hover();
  await tree.focus();
  await tree.press("ArrowRight");
  await expect(parentRow).toHaveAttribute("aria-expanded", "true");
  await expect(childLabel).toBeVisible();
  await expect(properties).toBeVisible();
  await expect(tree).toBeVisible();
  await saveEvidence(page, testInfo, `f10-context-location-keyboard-${viewportKey}`, {
    childVisible: await childLabel.isVisible(),
    destination: (await destination.innerText()).trim(),
    parentExpanded: await parentRow.getAttribute("aria-expanded"),
    popupVisible: await properties.isVisible(),
    treeVisible: await tree.isVisible(),
    viewport,
  });

  await parentRow.getByText(parent.title, { exact: true }).click();
  await expect(tree).toHaveCount(0);
  await expect(properties).toBeVisible();
  await expect(destination).toContainText(parent.title);
  await expect(destination).toBeFocused();

  const pageTitle = `${titlePrefix} page`;
  await page.getByLabel("Title", { exact: true }).fill(pageTitle);
  const createResponse = page.waitForResponse(
    (response) =>
      response.url()
        === `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/context/pages`
      && response.request().method() === "POST",
  );
  const editorBootstrap = waitForContextEditorBootstrap(page, workspaceId);
  await page.keyboard.press("Control+s");
  const saved = await responseData<SeededPage>(
    await createResponse,
    `save F10 Context page at ${viewportKey}`,
  );
  await expect(page).toHaveURL(new RegExp(`/context/${saved.slug}/edit`));
  await editorBootstrap;
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(pageTitle);
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(pageTitle);
  const placement = await pagePlacement(page.request, workspaceId, saved.id);
  expect(placement.parent_id).toBe(parent.id);
}

async function waitForContextEditorBootstrap(page: Page, workspaceId: string) {
  const workspacePrefix = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
  const responses = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname === `${workspacePrefix}/context/media/capabilities`,
    ),
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname === `${workspacePrefix}/tree`,
    ),
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname.startsWith(`${workspacePrefix}/library/items`),
    ),
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname
          === `${workspacePrefix}/context/pages`,
    ),
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET"
        && new URL(response.url()).pathname.startsWith(
          `${workspacePrefix}/context/pages/by-slug/`,
        ),
    ),
  ]);
  await Promise.all(
    responses.map(async (response) => {
      expect(response.ok(), `editor bootstrap ${new URL(response.url()).pathname}`).toBeTruthy();
      expect(await response.finished(), `editor bootstrap ${new URL(response.url()).pathname}`)
        .toBeNull();
    }),
  );
}

async function pagePlacement(
  request: APIRequestContext,
  workspaceId: string,
  pageId: string,
) {
  const payload = await responseData<
    | SeededTreeNode[]
    | { nodes: Array<SeededTreeNode & { target_id: string | null }> }
  >(
    await request.get(
      `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/tree?section=context`,
    ),
    "load Context tree",
  );
  const nodes = Array.isArray(payload) ? payload : payload.nodes;
  const placement = nodes.find(
    (node) => "target_id" in node && node.target_id === pageId,
  );
  expect(placement, `page ${pageId} must have a Context tree placement`).toBeTruthy();
  return placement!;
}

async function responseData<T>(response: APIResponse, label: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `${label} failed with ${response.status()}: ${await response.text()}`,
    );
  }
  return ((await response.json()) as { data: T }).data;
}

async function expectSplitGeometry(
  page: Page,
  orientation: "horizontal" | "vertical",
) {
  const grid = page.locator('.editor-grid[data-layout="split"]');
  const source = grid.locator(".source-pane");
  const separator = grid.locator(".editor-split-separator");
  const preview = grid.locator(".preview-pane");
  await expect(grid).toBeVisible();
  await expect(source).toBeVisible();
  await expect(separator).toHaveAttribute("aria-orientation", orientation);
  await expect(preview).toBeVisible();
  const boxes = {
    grid: requiredBox(await grid.boundingBox(), "split grid"),
    preview: requiredBox(await preview.boundingBox(), "split preview"),
    separator: requiredBox(await separator.boundingBox(), "split separator"),
    source: requiredBox(await source.boundingBox(), "split source"),
  };
  if (orientation === "vertical") {
    expect(boxes.source.width).toBeGreaterThanOrEqual(320);
    expect(boxes.preview.width).toBeGreaterThanOrEqual(320);
    expect(boxes.source.x + boxes.source.width).toBeLessThanOrEqual(
      boxes.separator.x + 1,
    );
    expect(boxes.separator.x + boxes.separator.width).toBeLessThanOrEqual(
      boxes.preview.x + 1,
    );
    expect(
      Math.abs(
        boxes.grid.width -
          (boxes.source.width + boxes.separator.width + boxes.preview.width),
      ),
    ).toBeLessThanOrEqual(2);
  } else {
    expect(boxes.source.height).toBeGreaterThanOrEqual(280);
    expect(boxes.preview.height).toBeGreaterThanOrEqual(280);
    expect(boxes.source.y + boxes.source.height).toBeLessThanOrEqual(
      boxes.separator.y + 1,
    );
    expect(boxes.separator.y + boxes.separator.height).toBeLessThanOrEqual(
      boxes.preview.y + 1,
    );
  }
  await expectNoHorizontalOverflow(page, `${orientation} split editor`);
  return boxes;
}

function monitorBrowserFailures(page: Page) {
  const failures: BrowserFailure[] = [];
  let expectedPreviewTeardown: { token: number; workspaceId: string } | null = null;
  let nextExpectedPreviewTeardownToken = 0;
  page.on("console", (message) => {
    // The requestfailed handler below already skips ERR_NETWORK_CHANGED; its
    // console echo is the same event and is skipped for the same reason.
    if (
      message.type() === "error" &&
      !isNetworkChangeConsoleEcho(message.text()) &&
      !isTransientNetworkImportError(page, message.text())
    ) {
      failures.push({ detail: message.text(), kind: "console" });
    }
  });
  page.on("pageerror", (error) => {
    if (!isTransientNetworkImportError(page, error.message)) {
      failures.push({ detail: error.message, kind: "pageerror" });
    }
  });
  page.on("requestfailed", (request) => {
    // Hyphenated so the sanitizer's SAFE_ERROR_TEXT keeps it; a space
    // would make the default case indistinguishable from a REDACTED one.
    const errorText = request.failure()?.errorText ?? "request-failed";
    if (errorText.includes("ERR_NETWORK_CHANGED")) {
      return;
    }
    const expectedAbort = isExpectedRequestAbort(errorText);
    if (
      expectedAbort &&
      isExpectedAuthProbeRequest(request.url(), request.method())
    ) {
      return;
    }
    if (
      expectedAbort &&
      expectedPreviewTeardown &&
      isExpectedPreviewRequest(request.url(), request.method(), expectedPreviewTeardown.workspaceId)
    ) {
      expectedPreviewTeardown = null;
      return;
    }
    // The whole record, not just the URL: redact-network.ts sanitizes
    // `errorText` too, because an engine is free to interpolate the failing
    // URL into it.
    const redacted = redactFailedRequest({
      errorText,
      method: request.method(),
      url: request.url(),
    });
    failures.push({
      detail: redacted.errorText,
      kind: "requestfailed",
      url: `${redacted.origin}${redacted.pathname}`,
    });
  });
  page.on("response", (response) => {
    if (response.status() >= 400 && response.status() !== 401) {
      failures.push({
        detail: `HTTP ${response.status()} ${response.statusText()}`,
        kind: "response",
        url: redactedUrl(response.url()),
      });
    }
  });
  return {
    armExpectedPreviewTeardown(workspaceId: string) {
      if (expectedPreviewTeardown) {
        throw new Error("An expected preview teardown request is already armed");
      }
      const token = ++nextExpectedPreviewTeardownToken;
      expectedPreviewTeardown = { token, workspaceId };
      return () => {
        if (expectedPreviewTeardown?.token === token) {
          expectedPreviewTeardown = null;
        }
      };
    },
    failures,
  } satisfies BrowserFailureMonitor;
}

function isExpectedRequestAbort(errorText: string) {
  return (
    errorText === "net::ERR_ABORTED" ||
    errorText === "Load request cancelled" ||
    errorText === "NS_BINDING_ABORTED"
  );
}

function isExpectedAuthProbeRequest(url: string, method: string) {
  return method === "GET" && new URL(url).pathname === "/api/v1/auth/me";
}

function isExpectedPreviewRequest(url: string, method: string, workspaceId: string) {
  if (method !== "GET") return false;
  const pathname = new URL(url).pathname;
  const previewPrefix =
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/library/`;
  return pathname.startsWith(previewPrefix) &&
    pathname.includes("/versions/") &&
    (pathname.endsWith("/preview") || pathname.includes("/preview/"));
}

async function assertBrowserFailuresClean(
  monitor: BrowserFailureMonitor,
  testInfo: TestInfo,
  name: string,
) {
  await saveJson(testInfo, name, monitor.failures);
  expect(monitor.failures, JSON.stringify(monitor.failures, null, 2)).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page, label: string) {
  const geometry = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(
    Math.max(geometry.body, geometry.document),
    `${label} document width`,
  ).toBeLessThanOrEqual(geometry.viewport + 1);
  return geometry;
}

async function surfaceGeometry(page: Page, selectors: string[]) {
  return page.evaluate((requestedSelectors) => {
    const toRect = (rect: DOMRect) => ({
      height: rect.height,
      width: rect.width,
      x: rect.x,
      y: rect.y,
    });
    return Object.fromEntries(
      requestedSelectors.map((selector) => {
        const element = document.querySelector<HTMLElement>(selector);
        if (!element) {
          return [selector, null];
        }
        const style = getComputedStyle(element);
        return [
          selector,
          {
            display: style.display,
            rect: toRect(element.getBoundingClientRect()),
            visibility: style.visibility,
          },
        ];
      }),
    );
  }, selectors);
}

async function saveEvidence(
  page: Page,
  testInfo: TestInfo,
  name: string,
  metadata: unknown,
) {
  const screenshotPath = await evidenceScreenshot(page, testInfo, name, {
    animations: "disabled",
    fullPage: false,
  });
  if (screenshotPath) {
    copyToCuratedArtifactDir(screenshotPath, `${name}.png`);
  }
  await saveJson(testInfo, `${name}.geometry`, metadata);
}

/**
 * The only URL shape anything in this file may retain.
 *
 * E2E-03 covered `tests/e2e/fixtures.ts`; this file is a SECOND artifact
 * writer that was outside it. `saveJson` below attaches BY PATH, so its
 * payload lands in `test-results/` and the HTML reporter copies it into
 * `playwright-report/data/`, which .github/workflows/ci.yml and
 * .github/workflows/e2e-browsers.yml both upload for 14 days -- the same
 * publication path, and therefore the same rule: method + origin + pathname,
 * never a query string.
 *
 * `redactFailedRequest` is imported rather than re-implemented so there is one
 * definition of that rule in the repo instead of two that can drift, and it is
 * already pinned behaviourally by tests/e2e/redact-network.test.mjs (including
 * the non-http scheme case, where `pathname` IS the payload, the
 * credential-prefix path scrub, and the per-field length clamp -- three
 * hardenings a second copy of the rule here would not have had).
 *
 * The dropped query is not lost evidence: every URL invariant this spec cares
 * about is asserted directly against the live URL --
 * `await expect(page).toHaveURL(/(?:\?|&)page=2(?:&|$)/)` and friends -- so
 * the attachment never was the thing establishing it.
 */
function redactedUrl(url: unknown): string {
  const { origin, pathname } = redactFailedRequest({ url });
  return `${origin}${pathname}`;
}

async function saveJson(testInfo: TestInfo, name: string, value: unknown) {
  const body = `${JSON.stringify(value, null, 2)}\n`;
  const jsonPath = testInfo.outputPath(`${name}.json`);
  mkdirSync(path.dirname(jsonPath), { recursive: true });
  writeFileSync(jsonPath, body, "utf8");
  await testInfo.attach(name, {
    contentType: "application/json",
    path: jsonPath,
  });
  copyToCuratedArtifactDir(jsonPath, `${name}.json`);
}

function copyToCuratedArtifactDir(sourcePath: string, filename: string) {
  const configuredDir = process.env.DTXWIKI_UI_FIX_ARTIFACT_DIR?.trim();
  if (!configuredDir) {
    return;
  }

  const curatedDir = path.resolve(configuredDir);
  const curatedPath = path.join(curatedDir, filename);
  if (path.resolve(sourcePath) === curatedPath) {
    return;
  }

  mkdirSync(curatedDir, { recursive: true });
  copyFileSync(sourcePath, curatedPath);
}

async function setEnglish(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem("dtxwiki.uiLanguage", "en");
  });
}

function fixtureSuffix(testInfo: TestInfo) {
  return [
    testInfo.project.name,
    testInfo.workerIndex,
    testInfo.retry,
    Date.now(),
  ]
    .join("-")
    .replace(/[^a-z0-9-]+/gi, "-")
    .toLowerCase();
}

function requiredBox(box: BoundingBox | null, label: string) {
  if (!box) {
    throw new Error(`${label} did not produce a browser layout box`);
  }
  return box;
}
