import { readFileSync } from "node:fs";

import {
  expect,
  test,
  type Page,
  type Response,
} from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace } from "./auth";

type LibraryVersion = {
  id: string;
  format_id: string | null;
  original_url: string | null;
  preview_status?: string;
  processing_status: string;
  selected_conversion_attempt_id: string | null;
  selected_preview_attempt_id?: string | null;
};

type LibraryArtifact = {
  index_text: string | null;
  library_item_version_id: string;
};

type LibraryItemDetail = {
  id: string;
  title: string | null;
  current_preview_version_id?: string | null;
  current_ready_version_id: string | null;
  current_version: LibraryVersion | null;
  artifacts: LibraryArtifact[];
};

type UploadedFixture = {
  content: string;
  detail: LibraryItemDetail;
  marker: string;
  name: string;
};

type SemanticMarkdownPayload = {
  base_artifact_id: string;
  body_markdown: string;
  content_sha256: string;
  overlay_id: string | null;
  revision: number;
  source: "base" | "overlay";
};

test("converts multi-uploaded JSON and CSV into durable Library previews", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);

  await authenticatePage(page, testInfo, "library-multiformat");
  const workspace = await currentWorkspace(page.request);
  const suffix = `${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}`;
  const jsonMarker = `json-marker-${suffix}`;
  const csvMarker = `csv-marker-${suffix}`;
  const fixtures = [
    {
      content: JSON.stringify(
        { marker: jsonMarker, nested: { count: 2 } },
        null,
        2,
      ),
      marker: jsonMarker,
      mimeType: "application/json",
      name: `e2e-library-${suffix}.json`,
    },
    {
      content: `kind,value\nmarker,${csvMarker}\ncount,2\n`,
      marker: csvMarker,
      mimeType: "text/csv",
      name: `e2e-library-${suffix}.csv`,
    },
  ] as const;
  await page.goto("/library");
  await expect(
    page.getByRole("heading", { name: "Library", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  const uploadBodies: Array<Promise<{ data: LibraryItemDetail }>> = [];
  const uploadEndpoint =
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/library/items`;
  const collectUploadResponse = (response: Response) => {
    if (
      response.url() === uploadEndpoint &&
      response.request().method() === "POST" &&
      response.ok()
    ) {
      uploadBodies.push(
        response.json() as Promise<{ data: LibraryItemDetail }>,
      );
    }
  };
  page.on("response", collectUploadResponse);
  try {
    await page.getByLabel("Upload Library file").setInputFiles(
      fixtures.map((fixture) => ({
        buffer: Buffer.from(fixture.content, "utf8"),
        mimeType: fixture.mimeType,
        name: fixture.name,
      })),
    );
    await expect.poll(() => uploadBodies.length).toBe(fixtures.length);
  } finally {
    page.off("response", collectUploadResponse);
  }

  const uploadResults = await Promise.all(uploadBodies);
  const uploaded: UploadedFixture[] = uploadResults.map((body) => {
    const fixture = fixtures.find(
      (candidate) => candidate.name === body.data.title,
    );
    expect(
      fixture,
      `uploaded item ${body.data.id} must retain its filename`,
    ).toBeTruthy();
    expect(body.data.current_version?.processing_status).toBe("queued");
    expect(body.data.current_version?.format_id).toBe(
      fixture?.name.endsWith(".json") ? "json" : "csv",
    );
    return {
      content: fixture!.content,
      detail: body.data,
      marker: fixture!.marker,
      name: fixture!.name,
    };
  });

  const uploadQueue = page.getByRole("region", { name: "Upload queue" });
  for (const fixture of fixtures) {
    await expect(
      uploadQueue.getByText(fixture.name, { exact: true }),
    ).toBeVisible();
  }
  await expect(
    uploadQueue.getByText("Succeeded", { exact: true }),
  ).toHaveCount(2);

  for (const fixture of uploaded) {
    fixture.detail = await waitForReady(page, workspace.id, fixture.detail.id);
    const readyVersion = fixture.detail.current_version;
    expect(readyVersion).toBeTruthy();
    expect(fixture.detail.current_ready_version_id).toBe(readyVersion?.id);
    expect(readyVersion?.selected_conversion_attempt_id).toBeTruthy();
    expect(
      fixture.detail.artifacts.some(
        (artifact) =>
          artifact.library_item_version_id === readyVersion?.id &&
          artifact.index_text?.includes(fixture.marker),
      ),
      `${fixture.name} must expose the selected canonical Markdown artifact`,
    ).toBeTruthy();
  }

  const editableFixture = uploaded.find((fixture) => fixture.name.endsWith(".json"));
  expect(editableFixture, "JSON fixture must be available for semantic editing").toBeTruthy();
  await assertSemanticMarkdownEdit(
    page,
    workspace.id,
    editableFixture!,
    `edited-semantic-${suffix}`,
  );

  for (const fixture of uploaded) {
    await assertPreviewAndOriginalSurviveReload(page, fixture);
    await expect(page).toHaveTitle(`${fixture.name} - dtxWiki`);
  }

  await page.getByLabel("Breadcrumb").getByRole("link", { name: "Library", exact: true }).click();
  await expect(page).toHaveTitle("dtxWiki");

  for (const fixture of uploaded) {
    const deleted = await page.request.delete(
      `${backendBaseUrl}/api/v1/workspaces/${workspace.id}/library/items/${fixture.detail.id}`,
    );
    expect(
      deleted.ok(),
      `converted Library item ${fixture.detail.id} must remain deletable`,
    ).toBeTruthy();
  }
});

test("renders protected image, PDF, WAV, and MP4 previews from exact-version sources", async ({
  page,
}, testInfo) => {
  test.setTimeout(90_000);

  await authenticatePage(page, testInfo, "library-preview-media");
  const workspace = await currentWorkspace(page.request);
  const suffix = `${testInfo.workerIndex}-${testInfo.retry}-${Date.now()}`;
  const imageName = `preview-${suffix}.png`;
  const pdfName = `preview-${suffix}.pdf`;
  const audioName = `preview-${suffix}.wav`;
  const videoName = `preview-${suffix}.mp4`;
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAYAAACzzX7wAAAAFUlEQVR4nGOU" +
      "q7jznwEPYMInSR0FAD1JAnlVwrsyAAAAAElFTkSuQmCC",
    "base64",
  );
  const pdf = minimalPdf("Protected Preview PDF", 2);
  const wav = minimalWav();
  const mp4 = minimalMp4();

  const image = await uploadLibraryFixture(
    page,
    workspace.id,
    imageName,
    "image/png",
    png,
  );
  const pdfItem = await uploadLibraryFixture(
    page,
    workspace.id,
    pdfName,
    "application/pdf",
    pdf,
  );
  const audio = await uploadLibraryFixture(
    page,
    workspace.id,
    audioName,
    "audio/wav",
    wav,
  );
  const video = await uploadLibraryFixture(
    page,
    workspace.id,
    videoName,
    "video/mp4",
    mp4,
  );

  expect(image.current_preview_version_id ?? null).toBeNull();
  expect(image.current_version?.preview_status).toBe("unavailable");

  const imageSource = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith(
        `/library/versions/${image.current_version?.id}/preview/source`,
      ),
  );
  await page.goto(`/library?target=${encodeURIComponent(image.id)}`);
  await expect(
    page.getByRole("heading", { level: 2, name: imageName, exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  const imageElement = page.getByRole("img", { name: imageName });
  await expect(imageElement).toBeVisible({ timeout: 10_000 });
  const imageResponse = await imageSource;
  expect(imageResponse.ok()).toBeTruthy();
  expect(imageResponse.headers()["accept-ranges"]).toBe("bytes");
  expect(imageResponse.headers()["content-security-policy"]).toBe(
    "default-src 'none'; sandbox",
  );
  expect(imageResponse.headers()["cross-origin-resource-policy"]).toBe(
    "same-origin",
  );
  await expect.poll(() => imageElement.evaluate((node) => (
    node instanceof HTMLImageElement ? node.naturalWidth : 0
  ))).toBeGreaterThan(0);
  await page.reload();
  await expect(page.getByRole("img", { name: imageName })).toBeVisible();

  const pdfSource = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith(
        `/library/versions/${pdfItem.current_version?.id}/preview/source`,
      ),
  );
  await page.goto(`/library?target=${encodeURIComponent(pdfItem.id)}`);
  await expect(page.getByLabel(`PDF preview for ${pdfName}`)).toBeVisible();
  await expect(page.locator(".react-pdf__Page__canvas")).toBeVisible();
  await expect(page.getByText("Protected Preview PDF", { exact: false })).toBeVisible();

  // The page is rasterised at a width the pane can actually show, and
  // react-pdf's selectable text layer is laid out from that same width. Pinning
  // the two together is what keeps the reading tab from scrolling sideways over
  // an invisible strip -- a fixed width left the text layer at 720px against a
  // canvas the stylesheet had squeezed to 508px here, 460px wider than the pane
  // on a phone.
  const pdfGeometry = await page.evaluate(() => {
    const documentPane = document.querySelector(".library-pdf-document");
    const canvas = document.querySelector(".react-pdf__Page__canvas");
    const textLayer = document.querySelector(".react-pdf__Page .textLayer");
    if (!documentPane || !canvas || !textLayer) {
      throw new Error("PDF preview is missing its document, canvas or text layer");
    }
    return {
      overflowX: documentPane.scrollWidth - documentPane.clientWidth,
      canvasWidth: Math.round(canvas.getBoundingClientRect().width),
      canvasHeight: Math.round(canvas.getBoundingClientRect().height),
      textLayerWidth: Math.round(textLayer.getBoundingClientRect().width),
      textLayerHeight: Math.round(textLayer.getBoundingClientRect().height),
    };
  });
  expect(pdfGeometry.overflowX).toBeLessThanOrEqual(1);
  expect(pdfGeometry.textLayerWidth).toBe(pdfGeometry.canvasWidth);
  expect(pdfGeometry.textLayerHeight).toBe(pdfGeometry.canvasHeight);

  // A real click, because the defect was that the bars could not receive one:
  // the text layer above carries pdf.js's `z-index: 2` and spans the whole
  // page, so at `z-index: 1` these bars took no clicks at all and `force: true`
  // did not turn the page either. Both directions, so a pager wired to the
  // wrong handler cannot pass by leaving the page where it started.
  const pageIndicator = page.locator(".library-pdf-page-indicator");
  await expect(pageIndicator).toHaveText("Page 1 / 2");
  await page.getByRole("button", { name: "Next PDF page" }).click();
  await expect(pageIndicator).toHaveText("Page 2 / 2");
  await expect(page.getByText("Protected Preview PDF page 2")).toBeVisible();
  await page.getByRole("button", { name: "Previous PDF page" }).click();
  await expect(pageIndicator).toHaveText("Page 1 / 2");

  // Phone width is a separate case, not a smaller one. Below 640px the document
  // drops its 64px side padding and the bars sit over the edges of the page
  // itself, so the text layer is above them whatever width the page renders at
  // -- the stacking order is the only thing keeping them clickable there.
  // Measured: fitting the page to the pane fixes desktop and tablet on its own
  // but leaves all three phone widths dead, so this is the half of the fix the
  // desktop check above cannot see.
  // Zoom has to move the CANVAS, not just the scroll extent. A `max-width:
  // 100%` on the canvas cancelled it silently: stepping to 200% widened the
  // text layer and the scrollable area while the bitmap stayed exactly where
  // it was, so the page looked identical and only the geometry gave it away.
  // Asserting the ratio and the text layer together is what pins that: the
  // ratio alone passes if only the layer moves, and the layer alone passes if
  // neither does.
  const zoomIn = page.getByRole("button", { name: "Zoom in on the PDF page" });
  await zoomIn.click();
  await expect(page.locator(".library-pdf-zoom-level")).toHaveText("125%");
  await expect(page.locator(".react-pdf__Page__canvas")).toBeVisible();
  await expect(page.getByText("Protected Preview PDF", { exact: false })).toBeVisible();
  const zoomedGeometry = await page.evaluate(() => {
    const canvas = document.querySelector(".react-pdf__Page__canvas");
    const textLayer = document.querySelector(".react-pdf__Page .textLayer");
    if (!canvas || !textLayer) {
      throw new Error("PDF preview is missing its canvas or text layer");
    }
    return {
      canvasWidth: Math.round(canvas.getBoundingClientRect().width),
      canvasHeight: Math.round(canvas.getBoundingClientRect().height),
      textLayerWidth: Math.round(textLayer.getBoundingClientRect().width),
      textLayerHeight: Math.round(textLayer.getBoundingClientRect().height),
    };
  });
  expect(zoomedGeometry.canvasWidth).toBeGreaterThan(pdfGeometry.canvasWidth);
  expect(zoomedGeometry.textLayerWidth).toBe(zoomedGeometry.canvasWidth);
  expect(zoomedGeometry.textLayerHeight).toBe(zoomedGeometry.canvasHeight);
  await page.getByRole("button", { name: "Zoom out of the PDF page" }).click();
  await expect(page.locator(".library-pdf-zoom-level")).toHaveText("100%");

  const desktopViewport = page.viewportSize();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".library-pdf-stage").scrollIntoViewIfNeeded();
  await page.getByRole("button", { name: "Next PDF page" }).click();
  await expect(pageIndicator).toHaveText("Page 2 / 2");
  await page.getByRole("button", { name: "Previous PDF page" }).click();
  await expect(pageIndicator).toHaveText("Page 1 / 2");
  if (desktopViewport) {
    await page.setViewportSize(desktopViewport);
  }
  const pdfResponse = await pdfSource;
  expect([200, 206]).toContain(pdfResponse.status());
  expect(pdfResponse.headers()["accept-ranges"]).toBe("bytes");
  expect(pdfResponse.headers()["content-security-policy"]).toBe(
    "default-src 'none'; sandbox",
  );
  expect(pdfResponse.headers()["cross-origin-resource-policy"]).toBe(
    "same-origin",
  );

  const audioVersionId = audio.current_version?.id;
  expect(audioVersionId, "WAV upload must expose its exact version").toBeTruthy();
  const audioSourceUrl =
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}` +
    `/library/versions/${audioVersionId}/preview/source`;
  const rangedAudioSource = await page.request.get(audioSourceUrl, {
    headers: { Range: "bytes=0-" },
  });
  expect(rangedAudioSource.status()).toBe(206);
  expect(rangedAudioSource.headers()["accept-ranges"]).toBe("bytes");
  expect(rangedAudioSource.headers()["content-range"]).toBe(
    `bytes 0-${wav.length - 1}/${wav.length}`,
  );
  expect(rangedAudioSource.headers()["cache-control"]).toBe("private, no-store");
  expect(rangedAudioSource.headers()["content-security-policy"]).toBe(
    "default-src 'none'; sandbox",
  );
  expect(rangedAudioSource.headers()["cross-origin-resource-policy"]).toBe(
    "same-origin",
  );
  expect((await rangedAudioSource.body()).equals(wav)).toBeTruthy();

  await page.goto(`/library?target=${encodeURIComponent(audio.id)}`);
  const audioElement = page.getByLabel(`Audio preview for ${audioName}`);
  const explicitAudioFallback = page.getByText(
    /^(This browser cannot play this media format|The media preview could not be played)\. The original file is safely stored\.$/,
  );
  await expect(
    audioElement.or(explicitAudioFallback).first(),
  ).toBeVisible();
  if (await audioElement.isVisible()) {
    await expect(audioElement).toHaveAttribute("controls", "");
    await expect(audioElement).toHaveAttribute("preload", "metadata");
    await expect(audioElement).not.toHaveAttribute("autoplay");
    // Resolved against the document, not compared raw: the packaged bundle
    // addresses the API relative to its own origin, so the attribute is a path
    // there and an absolute URL under a split-origin build. The product fact
    // being pinned -- which resource the element streams -- is the same one.
    await expect
      .poll(async () =>
        new URL(
          (await audioElement.getAttribute("src")) ?? "",
          page.url(),
        ).toString(),
      )
      .toBe(audioSourceUrl);
  } else {
    await expect(explicitAudioFallback).toBeVisible();
    await expect(page.getByRole("button", { name: "Download file" })).toBeVisible();
  }

  const videoVersionId = video.current_version?.id;
  expect(videoVersionId, "MP4 upload must expose its exact version").toBeTruthy();
  const videoSourceUrl =
    `${backendBaseUrl}/api/v1/workspaces/${workspace.id}` +
    `/library/versions/${videoVersionId}/preview/source`;
  const rangedVideoSource = await page.request.get(videoSourceUrl, {
    headers: { Range: "bytes=0-" },
  });
  expect(rangedVideoSource.status()).toBe(206);
  expect(rangedVideoSource.headers()["accept-ranges"]).toBe("bytes");
  expect(rangedVideoSource.headers()["content-range"]).toBe(
    `bytes 0-${mp4.length - 1}/${mp4.length}`,
  );
  expect(rangedVideoSource.headers()["cache-control"]).toBe("private, no-store");
  expect(rangedVideoSource.headers()["content-security-policy"]).toBe(
    "default-src 'none'; sandbox",
  );
  expect(rangedVideoSource.headers()["cross-origin-resource-policy"]).toBe(
    "same-origin",
  );
  expect((await rangedVideoSource.body()).equals(mp4)).toBeTruthy();

  await page.goto(`/library?target=${encodeURIComponent(video.id)}`);
  const videoElement = page.getByLabel(`Video preview for ${videoName}`);
  const explicitPlaybackFallback = page.getByText(
    /^(This browser cannot play this media format|The media preview could not be played)\. The original file is safely stored\.$/,
  );
  await expect(
    videoElement.or(explicitPlaybackFallback).first(),
  ).toBeVisible();
  if (await videoElement.isVisible()) {
    await expect(videoElement).toHaveAttribute("controls", "");
    await expect(videoElement).toHaveAttribute("preload", "metadata");
    await expect(videoElement).not.toHaveAttribute("autoplay");
    await expect
      .poll(async () =>
        new URL(
          (await videoElement.getAttribute("src")) ?? "",
          page.url(),
        ).toString(),
      )
      .toBe(videoSourceUrl);
  } else {
    await expect(explicitPlaybackFallback).toBeVisible();
    await expect(page.getByRole("button", { name: "Download file" })).toBeVisible();
  }
});

async function waitForReady(
  page: Page,
  workspaceId: string,
  libraryItemId: string,
) {
  await expect.poll(
    async () => {
      const detail = await readLibraryDetail(page, workspaceId, libraryItemId);
      return detail.current_version?.processing_status ?? null;
    },
    { intervals: [100, 250, 500, 1_000], timeout: 20_000 },
  ).toBe("ready");
  return readLibraryDetail(page, workspaceId, libraryItemId);
}

async function assertPreviewAndOriginalSurviveReload(
  page: Page,
  fixture: UploadedFixture,
) {
  const originalUrl = fixture.detail.current_version?.original_url;
  expect(
    originalUrl,
    `${fixture.name} must expose its protected original URL`,
  ).toBeTruthy();

  await page.goto(`/library?target=${encodeURIComponent(fixture.detail.id)}`);
  await assertReadyPreview(page, fixture);

  await page.reload();
  const downloadButton = await assertReadyPreview(page, fixture);

  const originalResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url() === `${backendBaseUrl}${originalUrl}`,
  );
  const downloadPromise = page.waitForEvent("download");
  await downloadButton.click();
  const originalResponse = await originalResponsePromise;
  expect(originalResponse.ok()).toBeTruthy();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(fixture.name);
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  expect(
    readFileSync(downloadPath!).equals(Buffer.from(fixture.content, "utf8")),
    `${fixture.name} browser download must match the uploaded original bytes`,
  ).toBeTruthy();
}

async function assertReadyPreview(page: Page, fixture: UploadedFixture) {
  await expect(
    page.getByRole("heading", { level: 2, name: fixture.name, exact: true }),
  ).toBeVisible();
  await expect(
    page.locator('section.detail-section .status-pill[title="ready"]'),
  ).toBeVisible();
  await expect(
    page.getByRole("tabpanel", { name: "View" }).getByText(fixture.marker),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload", exact: true }))
    .toHaveCount(0);
  const deleteAction = page.getByRole("button", { name: "Delete", exact: true });
  await expect(deleteAction).toHaveCount(1);
  await expect(deleteAction).toHaveClass(/\bdanger\b/);
  await expect(deleteAction).toHaveClass(/\blibrary-reader-delete-action\b/);
  const showDetails = page.getByRole("button", { name: "Show Details" });
  if (await showDetails.isVisible()) {
    await showDetails.click();
  }
  const downloadButton = page.getByRole("button", {
    name: "Download original file",
  });
  await expect(downloadButton).toBeVisible();
  await expect(downloadButton).toBeEnabled();
  return downloadButton;
}

async function readLibraryDetail(
  page: Page,
  workspaceId: string,
  libraryItemId: string,
): Promise<LibraryItemDetail> {
  const response = await page.request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/library/items/${libraryItemId}`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).data as LibraryItemDetail;
}

async function assertSemanticMarkdownEdit(
  page: Page,
  workspaceId: string,
  fixture: UploadedFixture,
  editedMarker: string,
) {
  const versionId = fixture.detail.current_ready_version_id;
  expect(versionId, "editable fixture must have a current-ready version").toBeTruthy();

  await page.goto(`/library?target=${encodeURIComponent(fixture.detail.id)}`);
  const editAction = page.getByRole("button", { name: "Edit Markdown" });
  await expect(editAction).toBeVisible();
  await editAction.click();

  const editor = page.locator(".library-semantic-editor");
  await expect(editor).toBeVisible();
  await expect(editor.getByRole("button", { name: /review/i })).toHaveCount(0);
  const source = editor.getByRole("textbox", { name: "Markdown body" });
  const editedBody = `${fixture.content}\n\n## ${editedMarker}\n\n한글 편집 ✅`;
  await source.fill(editedBody);

  const saveResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().endsWith(
        `/library/versions/${versionId}/semantic-markdown`,
      ),
  );
  await editor.getByRole("button", { name: "Save", exact: true }).click();
  expect((await saveResponse).status()).toBe(200);
  await expect(editor.getByText("Saved", { exact: true })).toBeVisible();

  await editor.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("tabpanel", { name: "View" }).getByText(editedMarker))
    .toBeVisible();

  const semanticResponse = await page.request.get(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}` +
      `/library/versions/${versionId}/semantic-markdown`,
  );
  expect(semanticResponse.ok()).toBeTruthy();
  const semantic = (await semanticResponse.json()).data as SemanticMarkdownPayload;
  expect(semantic.source).toBe("overlay");
  expect(semantic.revision).toBe(1);
  expect(semantic.overlay_id).toBeTruthy();
  expect(semantic.body_markdown).toContain(editedMarker);
  expect(semantic.content_sha256).toMatch(/^[0-9a-f]{64}$/);
}

async function uploadLibraryFixture(
  page: Page,
  workspaceId: string,
  name: string,
  mimeType: string,
  buffer: Buffer,
) {
  const response = await page.request.post(
    `${backendBaseUrl}/api/v1/workspaces/${workspaceId}/library/items`,
    { multipart: { file: { name, mimeType, buffer } } },
  );
  const payload = await response.json() as {
    data?: LibraryItemDetail;
    error_code?: string;
    message?: string;
  };
  expect(
    response.status(),
    `${name} upload failed: ${JSON.stringify(payload)}`,
  ).toBe(201);
  expect(payload.data, `${name} upload response must include data`).toBeTruthy();
  return payload.data!;
}

function minimalWav() {
  const samples = Buffer.alloc(160);
  const format = Buffer.alloc(16);
  format.writeUInt16LE(1, 0);
  format.writeUInt16LE(1, 2);
  format.writeUInt32LE(8_000, 4);
  format.writeUInt32LE(16_000, 8);
  format.writeUInt16LE(2, 12);
  format.writeUInt16LE(16, 14);
  const body = Buffer.concat([
    Buffer.from("fmt "),
    uint32le(format.length),
    format,
    Buffer.from("data"),
    uint32le(samples.length),
    samples,
  ]);
  return Buffer.concat([
    Buffer.from("RIFF"),
    uint32le(body.length + 4),
    Buffer.from("WAVE"),
    body,
  ]);
}

function minimalMp4(codec = "avc1") {
  const ftyp = mp4Box("ftyp", Buffer.concat([
    Buffer.from("isom"),
    Buffer.alloc(4),
    Buffer.from("isom"),
  ]));
  const handler = mp4Box("hdlr", Buffer.concat([
    Buffer.alloc(8),
    Buffer.from("vide"),
    Buffer.alloc(4),
  ]));
  const sample = Buffer.concat([
    uint32be(16),
    Buffer.from(codec, "ascii"),
    Buffer.alloc(8),
  ]);
  const sampleTable = mp4Box("stsd", Buffer.concat([
    Buffer.alloc(4),
    uint32be(1),
    sample,
  ]));
  const track = mp4Box(
    "trak",
    mp4Box(
      "mdia",
      Buffer.concat([
        handler,
        mp4Box("minf", mp4Box("stbl", sampleTable)),
      ]),
    ),
  );
  return Buffer.concat([
    ftyp,
    mp4Box("moov", track),
    mp4Box("mdat", Buffer.alloc(1)),
  ]);
}

function mp4Box(boxType: string, payload: Buffer) {
  return Buffer.concat([
    uint32be(8 + payload.length),
    Buffer.from(boxType, "ascii"),
    payload,
  ]);
}

// `pageCount` defaults to one page. The preview fixture asks for two so the
// reading tab's Previous/Next bars are both reachable: a one-page document
// renders them disabled, which is how the pager went untested while it was
// completely unclickable.
function minimalPdf(text: string, pageCount = 1) {
  const escape = (value: string) =>
    value.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
  // 1 = catalog, 2 = page tree, then (page, contents) per page, font last.
  const fontNumber = 3 + pageCount * 2;
  const kids = Array.from(
    { length: pageCount },
    (_unused, index) => `${3 + index * 2} 0 R`,
  ).join(" ");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Kids [${kids}] /Count ${pageCount} >>`,
  ];
  for (let index = 0; index < pageCount; index += 1) {
    // Page one keeps the caller's text verbatim so assertions on it still hold.
    const label = index === 0 ? text : `${text} page ${index + 1}`;
    const content = `BT /F1 14 Tf 24 100 Td (${escape(label)}) Tj ET\n`;
    objects.push(
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 140] /Resources " +
        `<< /Font << /F1 ${fontNumber} 0 R >> >> /Contents ${4 + index * 2} 0 R >>`,
    );
    objects.push(
      `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}endstream`,
    );
  }
  objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  let body = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xrefOffset = Buffer.byteLength(body);
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  body += offsets.slice(1).map(
    (offset) => `${String(offset).padStart(10, "0")} 00000 n \n`,
  ).join("");
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n`;
  body += `startxref\n${xrefOffset}\n%%EOF\n`;
  return Buffer.from(body, "ascii");
}

function uint32le(value: number) {
  const buffer = Buffer.alloc(4);
  buffer.writeUInt32LE(value);
  return buffer;
}

function uint32be(value: number) {
  const buffer = Buffer.alloc(4);
  buffer.writeUInt32BE(value);
  return buffer;
}
