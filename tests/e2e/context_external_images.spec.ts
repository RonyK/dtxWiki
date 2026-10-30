import { expect, test } from "./fixtures";

import {
  backendBaseUrl,
  currentWorkspace,
  signUp,
  workspaceUrl,
} from "./auth";

const TWO_BY_TWO_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR42mOssXrLwMDAxMDAwMDAAAATXQGnHH/cKwAAAABJRU5ErkJggg==",
  "base64",
);

test("trusted Context reader loads HTTPS images and contains external failures @cross-browser", async ({
  page,
  browserName,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const successfulUrl = "https://images.example.test/ok.png";
  const brokenUrl = "https://images.example.test/broken.png";
  const unsafeUrl = "http://images.example.test/unsafe.png";
  let successfulRequests = 0;
  let brokenRequests = 0;
  let unsafeRequests = 0;

  await page.route(successfulUrl, async (route) => {
    successfulRequests += 1;
    await route.fulfill({
      body: TWO_BY_TWO_PNG,
      contentType: "image/png",
      status: 200,
    });
  });
  await page.route(brokenUrl, async (route) => {
    brokenRequests += 1;
    await route.fulfill({ status: 404 });
  });
  await page.route(unsafeUrl, async (route) => {
    unsafeRequests += 1;
    await route.fulfill({
      body: TWO_BY_TWO_PNG,
      contentType: "image/png",
      status: 200,
    });
  });

  await page.addInitScript(() => {
    const scope = window as Window & {
      __dtxCspViolations?: Array<{ blockedUri: string; directive: string }>;
    };
    scope.__dtxCspViolations = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      scope.__dtxCspViolations?.push({
        blockedUri: event.blockedURI,
        directive: event.effectiveDirective,
      });
    });
  });

  await signUp(
    page.request,
    `external-images-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  const workspace = await currentWorkspace(page.request);
  const stamp = Date.now();
  const createResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        body_md: [
          "# External image boundary",
          "",
          `![Secure remote diagram](${successfulUrl})`,
          "",
          `![Broken remote diagram](${brokenUrl})`,
          "",
          `![Unsafe transport](${unsafeUrl})`,
        ].join("\n"),
        page_type: "concept",
        tags: ["e2e"],
        title: `External image boundary ${stamp}`,
      },
    },
  );
  expect(createResponse.ok()).toBeTruthy();
  const created = (await createResponse.json()).data as { id: string };

  await page.goto(workspaceUrl(workspace.id, `context/pages/${created.id}`));
  const article = page.getByRole("article");
  await expect(article).toBeVisible();

  const loadedImage = article.getByRole("img", {
    name: "Secure remote diagram",
  });
  await loadedImage.scrollIntoViewIfNeeded();
  await expect(loadedImage).toHaveAttribute("data-load-state", "loaded");
  await expect(loadedImage).toHaveAttribute("referrerpolicy", "no-referrer");
  await expect(loadedImage).not.toHaveAttribute("crossorigin", /.+/);
  expect(
    await loadedImage.evaluate((image: HTMLImageElement) => ({
      height: image.naturalHeight,
      width: image.naturalWidth,
    })),
  ).toEqual({ height: 2, width: 2 });
  expect(successfulRequests).toBe(1);

  const brokenPlaceholder = article.locator(".markdown-image-failure", {
    hasText: "Broken remote diagram",
  });
  await brokenPlaceholder.scrollIntoViewIfNeeded();
  await expect(brokenPlaceholder).toBeVisible();
  await expect(brokenPlaceholder).toContainText(
    "Alt text: Broken remote diagram",
  );
  await expect(brokenPlaceholder).toContainText(
    "Source: images.example.test",
  );
  const placeholderBounds = await brokenPlaceholder.boundingBox();
  expect(placeholderBounds).not.toBeNull();
  expect(placeholderBounds!.height).toBeGreaterThan(0);
  expect(placeholderBounds!.x).toBeGreaterThanOrEqual(0);
  expect(placeholderBounds!.x + placeholderBounds!.width).toBeLessThanOrEqual(
    391,
  );

  const retry = brokenPlaceholder.getByRole("button", {
    name: "Retry loading Broken remote diagram",
  });
  const openOriginal = brokenPlaceholder.getByRole("link", {
    name: "Open original image: Broken remote diagram",
  });
  await expect(retry).toBeVisible();
  await expect(openOriginal).toHaveAttribute("href", brokenUrl);
  await expect(openOriginal).toHaveAttribute("target", "_blank");
  await expect(openOriginal).toHaveAttribute("rel", "noopener noreferrer");
  await expect(openOriginal).toHaveAttribute("referrerpolicy", "no-referrer");
  const [retryBounds, openOriginalBounds] = await Promise.all([
    retry.boundingBox(),
    openOriginal.boundingBox(),
  ]);
  expect(retryBounds).not.toBeNull();
  expect(openOriginalBounds).not.toBeNull();
  expect(retryBounds!.height).toBeGreaterThanOrEqual(44);
  expect(openOriginalBounds!.height).toBeGreaterThanOrEqual(44);

  // Safari keeps links out of the Tab order unless the reader has turned on
  // Full Keyboard Access, so this sequence describes the platform rather than
  // our markup there. Everything else in this test -- containment, the
  // referrer policy, the 44px targets, retry -- is asserted on every engine.
  if (browserName !== "webkit") {
    await retry.press("Tab");
    await expect(openOriginal).toBeFocused();
    await openOriginal.press("Shift+Tab");
    await expect(retry).toBeFocused();
  }

  await retry.press("Enter");
  await expect.poll(() => brokenRequests).toBe(2);
  await expect(retry).toBeVisible();
  await retry.press("Enter");
  await expect.poll(() => brokenRequests).toBe(3);
  await expect(retry).toHaveCount(0);
  await expect(brokenPlaceholder).toContainText("Retry limit reached.");

  await expect(article.locator(`img[src=${JSON.stringify(unsafeUrl)}]`)).toHaveCount(0);
  await expect(article.getByRole("img", { name: "Unsafe transport" })).toHaveCount(0);
  expect(unsafeRequests).toBe(0);

  const cspViolations = await page.evaluate(() => {
    const scope = window as Window & {
      __dtxCspViolations?: Array<{ blockedUri: string; directive: string }>;
    };
    return scope.__dtxCspViolations ?? [];
  });
  expect(cspViolations).toEqual([]);
});
