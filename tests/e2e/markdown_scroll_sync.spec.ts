import { expect, test, type Locator, type Page } from "./fixtures";

import { backendBaseUrl, currentWorkspace, signUp, workspaceUrl } from "./auth";

const TALL_IMAGE_URL = "https://scroll-sync.test/tall.svg";

test("keeps split Markdown panes aligned by content in both directions @cross-browser", async ({
  page,
}, testInfo) => {
  const imageGate = deferred();
  await page.route(TALL_IMAGE_URL, async (route) => {
    await imageGate.promise;
    await route.fulfill({
      body: [
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1000"',
        ' viewBox="0 0 600 1000">',
        '<rect width="600" height="1000" fill="#dbeafe"/>',
        '<text x="300" y="500" text-anchor="middle" font-size="32">',
        "Delayed geometry",
        "</text></svg>",
      ].join(""),
      contentType: "image/svg+xml",
      status: 200,
    });
  });

  await signUp(
    page.request,
    `markdown-scroll-${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}@example.com`,
  );
  const workspace = await currentWorkspace(page.request);
  const createResponse = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/context/pages`,
    {
      data: {
        title: "Markdown scroll synchronization",
        body_md: scrollFixture(),
        page_type: "concept",
        tags: ["e2e", "scroll-sync"],
      },
    },
  );
  expect(createResponse.ok()).toBeTruthy();
  const created = (await createResponse.json()).data as { id: string; slug: string };

  await page.goto(workspaceUrl(workspace.id, `context/pages/${created.id}/edit`), { waitUntil: "domcontentloaded" });
  await page.getByLabel("Markdown body").waitFor();
  await page.getByRole("tab", { name: "Split" }).click();
  await expect(page.getByRole("button", { name: "Linked scrolling" }))
    .toHaveAttribute("aria-pressed", "true");

  const source = page.locator(".cm-scroller");
  const preview = page.locator(".preview-pane");
  await expect(preview).toHaveAttribute("tabindex", "0");
  await expect(preview.locator(`[src="${TALL_IMAGE_URL}"]`)).toHaveCount(1);

  await setScrollRatio(source, 0.55);
  await expect.poll(() => scrollTop(preview)).toBeGreaterThan(0);
  const sourceBeforeImage = await scrollTop(source);
  const previewBeforeImage = await scrollTop(preview);

  imageGate.resolve();
  const image = preview.locator(`[src="${TALL_IMAGE_URL}"]`);
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => (
    element.complete && element.naturalHeight > 0
  ))).toBe(true);
  await expect.poll(async () =>
    Math.abs((await scrollTop(preview)) - previewBeforeImage),
  ).toBeGreaterThan(300);
  expect(await scrollTop(source)).toBeCloseTo(sourceBeforeImage, 0);

  await setScrollRatio(source, 0.55);
  await page.waitForTimeout(100);
  const sourceDriven = await semanticAlignment(page);
  expect(Math.abs(sourceDriven.sourceLine - sourceDriven.previewLine))
    .toBeLessThan(6);

  await setScrollRatio(preview, 0.35);
  await page.waitForTimeout(100);
  const previewDriven = await semanticAlignment(page);
  expect(Math.abs(previewDriven.sourceLine - previewDriven.previewLine))
    .toBeLessThan(6);

  await setScrollRatio(source, 1);
  await expect.poll(() => scrollRatio(preview)).toBeGreaterThan(0.995);

  await setScrollRatio(preview, 0);
  await expect.poll(() => scrollRatio(source)).toBeLessThan(0.005);

  await setScrollRatio(preview, 1);
  await expect.poll(() => scrollRatio(source)).toBeGreaterThan(0.995);
});

async function setScrollRatio(locator: Locator, ratio: number) {
  await locator.evaluate((element, nextRatio) => {
    element.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
    const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
    element.scrollTop = maximum * nextRatio;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  }, ratio);
}

async function scrollTop(locator: Locator) {
  return locator.evaluate((element) => element.scrollTop);
}

async function scrollRatio(locator: Locator) {
  return locator.evaluate((element) => {
    const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
    return maximum === 0 ? 0 : element.scrollTop / maximum;
  });
}

async function semanticAlignment(page: Page) {
  return page.evaluate(() => {
    const source = document.querySelector<HTMLElement>(".cm-scroller")!;
    const preview = document.querySelector<HTMLElement>(".preview-pane")!;

    function viewportFraction(element: HTMLElement) {
      const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
      if (maximum === 0 || element.clientHeight === 0) {
        return 0;
      }
      const top = Math.max(0, Math.min(maximum, element.scrollTop));
      const edgeRange = Math.min(element.clientHeight / 2, maximum / 2);
      if (top < edgeRange) {
        return 0.5 * (top / edgeRange);
      }
      if (top > maximum - edgeRange) {
        return 0.5 + 0.5 * ((top - (maximum - edgeRange)) / edgeRange);
      }
      return 0.5;
    }

    const sourceRect = source.getBoundingClientRect();
    const sourceY = sourceRect.top +
      viewportFraction(source) * source.clientHeight;
    const gutterLines = [...document.querySelectorAll<HTMLElement>(
      ".cm-lineNumbers .cm-gutterElement",
    )]
      .map((element) => ({
        line: Number(element.textContent?.trim()),
        rect: element.getBoundingClientRect(),
      }))
      .filter((candidate) => Number.isFinite(candidate.line));
    const sourceLine = gutterLines.reduce((closest, candidate) => {
      const distance = Math.abs(
        (candidate.rect.top + candidate.rect.bottom) / 2 - sourceY,
      );
      return distance < closest.distance
        ? { distance, line: candidate.line }
        : closest;
    }, { distance: Number.POSITIVE_INFINITY, line: 1 }).line;

    const previewRect = preview.getBoundingClientRect();
    const previewY = previewRect.top +
      viewportFraction(preview) * preview.clientHeight;
    const previewLine = [...preview.querySelectorAll<HTMLElement>(
      '[data-scroll-sync-anchor="true"]',
    )].reduce((closest, element) => {
      const rect = element.getBoundingClientRect();
      const startLine = Number(element.dataset.sourceLine);
      const endBoundary = Number(element.dataset.sourceEndLine ?? startLine) + 1;
      const fraction = rect.height > 0
        ? Math.max(0, Math.min(1, (previewY - rect.top) / rect.height))
        : 0;
      const distance = previewY < rect.top
        ? rect.top - previewY
        : previewY > rect.bottom
          ? previewY - rect.bottom
          : 0;
      const line = startLine + fraction * (endBoundary - startLine);
      return distance < closest.distance ? { distance, line } : closest;
    }, { distance: Number.POSITIVE_INFINITY, line: 1 }).line;

    return {
      previewLine,
      sourceLine,
    };
  });
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function scrollFixture() {
  const lines = [
    "# Markdown scroll synchronization",
    "",
    "Introductory paragraph before the delayed image.",
    "",
    `![Delayed tall image](${TALL_IMAGE_URL})`,
    "",
  ];
  for (let index = 1; index <= 48; index += 1) {
    lines.push(
      `## Section ${index}`,
      "",
      `Section ${index} carries ${"asymmetric rendered content ".repeat(
        index % 5 === 0 ? 14 : 3,
      )}`,
      "",
    );
  }
  return lines.join("\n");
}
