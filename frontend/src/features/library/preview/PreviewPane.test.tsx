import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  LibraryItemDetail,
  LibraryItemVersionPayload,
} from "../../../shared/api/library";

const previewApi = vi.hoisted(() => ({
  getManifest: vi.fn(),
}));

vi.mock("../../../shared/api/libraryPreview", () => ({
  getLibraryPreviewManifest: previewApi.getManifest,
  isPreviewManifestEndpointUnavailable: () => false,
}));

import { previewVersionId, PreviewPane } from "./PreviewPane";

describe("PreviewPane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    previewApi.getManifest.mockReset();
    window.__DTXWIKI_RUNTIME_CONFIG__ = {};
  });

  it("selects a trusted renderer from the exact-version manifest", async () => {
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "ready",
      version_id: "version-1",
      selected_preview_attempt_id: "attempt-1",
      variants: [
        {
          kind: "remote-plugin",
          media_type: "application/x-plugin",
          delivery: "inline",
          renderer_contract: "plugin.remote.v1",
          rank: 10,
        },
        {
          kind: "text",
          media_type: "text/plain",
          delivery: "inline",
          renderer_contract: "core.text.v1",
          rank: 20,
          text: "Manifest-selected preview",
        },
      ],
      fallbacks: ["metadata", "download"],
    });

    render(
      <PreviewPane
        detail={libraryDetail()}
        legacyPreviewKind="pdf"
        workspaceId="workspace-1"
      />,
    );

    expect(await screen.findByText("Manifest-selected preview")).toBeInTheDocument();
    await waitFor(() => expect(previewApi.getManifest).toHaveBeenCalledTimes(1));
    expect(previewApi.getManifest).toHaveBeenCalledWith(
      "workspace-1",
      "version-1",
      expect.any(AbortSignal),
    );
  });

  it("advances deterministically when a trusted renderer rejects its payload", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "ready_partial",
      version_id: "version-1",
      selected_preview_attempt_id: "attempt-1",
      variants: [
        {
          kind: "table",
          media_type: "application/json",
          delivery: "inline",
          renderer_contract: "core.table.v1",
          rank: 10,
          data: { malformed: true },
        },
        {
          kind: "text",
          media_type: "text/plain",
          delivery: "inline",
          renderer_contract: "core.text.v1",
          rank: 20,
          text: "Safe fallback preview",
        },
      ],
      fallbacks: [],
    });

    render(
      <PreviewPane
        detail={libraryDetail()}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(await screen.findByText("Safe fallback preview")).toBeInTheDocument();
  });

  it("explains that the original is stored without exposing provider diagnostics", async () => {
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "unavailable",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [
        {
          kind: "metadata",
          media_type: "application/json",
          delivery: "inline",
          renderer_contract: "core.metadata.v1",
          rank: 10,
          data: {
            reason: "preview_provider_unavailable",
            provider_id: "internal.preview.provider",
          },
        },
      ],
      fallbacks: ["metadata", "download"],
      unavailable_reason: "preview_provider_unavailable",
      download_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
    });

    render(
      <PreviewPane
        detail={libraryDetail()}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(
      await screen.findByText(
        "No inline preview is available. The original file is safely stored.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(screen.queryByLabelText("File metadata preview")).toBeNull();
    expect(document.body).not.toHaveTextContent("preview_provider_unavailable");
    expect(document.body).not.toHaveTextContent("internal.preview.provider");
  });

  it("reports a failed conversion while keeping the stored original downloadable", async () => {
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "unavailable",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [],
      fallbacks: ["metadata", "download"],
      unavailable_reason: "converter_process_failed",
      download_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
    });

    render(
      <PreviewPane
        detail={libraryDetail("failed")}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(
      await screen.findByText("File saved, but document conversion failed."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(screen.queryByLabelText("File metadata preview")).toBeNull();
    expect(document.body).not.toHaveTextContent("converter_process_failed");
  });

  it.each([
    {
      formatHint: "html",
      itemFormatHint: "html",
      variantKind: "synthesized",
    },
    {
      formatHint: "javascript",
      itemFormatHint: "pdf",
      variantKind: "explicit",
    },
    {
      formatHint: "docx",
      itemFormatHint: "docx",
      variantKind: "synthesized",
    },
    {
      formatHint: "pptx",
      itemFormatHint: "pptx",
      variantKind: "explicit",
    },
    {
      formatHint: "xlsx",
      itemFormatHint: "pdf",
      variantKind: "explicit",
    },
  ])("does not expose a $formatHint $variantKind download that bypasses the warning", async ({
    formatHint,
    itemFormatHint,
    variantKind,
  }) => {
    const explicitVariant = {
      kind: "download",
      media_type: "application/octet-stream",
      delivery: "download",
      renderer_contract: "core.download.v1",
      rank: 10,
      content_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
    };
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "unavailable",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: variantKind === "explicit" ? [explicitVariant] : [],
      fallbacks: variantKind === "synthesized" ? ["download"] : [],
      download_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
    });
    const detail = libraryDetail("failed");
    detail.format_hint = itemFormatHint ?? formatHint;
    detail.trust_level = "unknown";
    detail.versions[0].format_id = formatHint;

    render(
      <PreviewPane
        detail={detail}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(
      await screen.findByText("No supported preview is available for this file."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
  });

  it("distinguishes a safe conversion timeout from Preview provider status", async () => {
    previewApi.getManifest.mockResolvedValue({
      schema_version: "1",
      status: "unavailable",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [],
      fallbacks: ["download"],
      unavailable_reason: "preview_provider_unavailable",
      download_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
    });
    const detail = libraryDetail("timed_out");
    detail.versions[0].conversion = {
      status: "timed_out",
      reason: "document_conversion_timed_out",
      retryable: true,
    };
    if (detail.current_version) {
      detail.current_version.conversion = detail.versions[0].conversion;
    }

    render(
      <PreviewPane
        detail={detail}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(
      await screen.findByText(
        "File saved, but document conversion timed out. You can still download the original.",
      ),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("preview_provider_unavailable");
  });

  it.each([
    {
      status: "failed" as const,
      reason: "document_conversion_failed" as const,
      retryable: false,
      notice: "File saved, but document conversion failed.",
    },
    {
      status: "timed_out" as const,
      reason: "document_conversion_timed_out" as const,
      retryable: true,
      notice:
        "File saved, but document conversion timed out. You can still download the original.",
    },
  ])("shows the current version's own $status notice instead of a previous ready version's content", async ({
    status,
    reason,
    retryable,
    notice,
  }) => {
    // §3-D: the old `current_preview_version_id`/`current_ready_version_id`
    // bridge that quietly substituted a previous READY version's content
    // when the latest one's conversion terminally failed is gone. The
    // manifest is now always fetched for the latest `current_version_id`,
    // and that version's own failure notice is what shows -- never someone
    // else's stale content standing in for it.
    previewApi.getManifest.mockImplementation((_workspaceId, versionId) =>
      Promise.resolve({
        schema_version: "1" as const,
        status: "unavailable" as const,
        version_id: versionId,
        selected_preview_attempt_id: null,
        variants: [],
        fallbacks: ["download"],
        download_url: `/api/v1/workspaces/workspace-1/library/versions/${versionId}/content`,
      }),
    );
    const detail = libraryDetail();
    const previousVersion = detail.versions[0];
    const currentVersion: LibraryItemVersionPayload = {
      ...previousVersion,
      id: "version-2",
      version_no: 2,
      processing_status: status,
      previous_version_id: "version-1",
      original_url:
        "/api/v1/workspaces/workspace-1/library/versions/version-2/content",
      conversion: {
        status,
        reason,
        retryable,
      },
    };
    detail.current_version_id = "version-2";
    detail.current_preview_version_id = null;
    detail.current_ready_version_id = "version-1";
    detail.current_ready_version = previousVersion;
    detail.current_version = currentVersion;
    detail.versions = [currentVersion, previousVersion];

    render(
      <PreviewPane
        detail={detail}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(screen.queryByText("Previous searchable content")).toBeNull();
    expect(
      screen.queryByText("Showing a preview from the previous available version."),
    ).toBeNull();
    expect(previewApi.getManifest).toHaveBeenCalledWith(
      "workspace-1",
      "version-2",
      expect.any(AbortSignal),
    );
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
  });

  it("uses a localized authentication fallback for token-only native media", async () => {
    window.__DTXWIKI_RUNTIME_CONFIG__ = { apiToken: "preview-token" };
    previewApi.getManifest.mockResolvedValue(mediaManifest());

    render(
      <PreviewPane
        detail={libraryDetail()}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(
      await screen.findByText(
        "Inline media preview is unavailable with the current sign-in method. The original file is safely stored.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("authentication");
  });

  it.each([
    {
      confidence: "" as const,
      errorCode: null,
      message:
        "This browser cannot play this media format. The original file is safely stored.",
    },
    {
      confidence: "probably" as const,
      errorCode: 2,
      message:
        "The media preview could not be played. The original file is safely stored.",
    },
  ])("uses a localized native media fallback after a playback error", async ({
    confidence,
    errorCode,
    message,
  }) => {
    vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue(confidence);
    previewApi.getManifest.mockResolvedValue(mediaManifest());

    render(
      <PreviewPane
        detail={libraryDetail()}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    const audio = await screen.findByLabelText("Audio preview for Manifest item");
    if (errorCode !== null) {
      Object.defineProperty(audio, "error", {
        configurable: true,
        value: { code: errorCode },
      });
    }
    fireEvent.error(audio);

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("preview_content_unavailable");
  });

  describe("original/converted rendition toggle", () => {
    it("does not show a toggle when only one rendition has a candidate", async () => {
      previewApi.getManifest.mockResolvedValue(mediaManifest());

      render(
        <PreviewPane
          detail={libraryDetail()}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );

      await screen.findByLabelText("Audio preview for Manifest item");
      expect(screen.queryByRole("group", { name: "Rendition" })).toBeNull();
    });

    it("defaults to the inline original and switches to the converted output on request", async () => {
      previewApi.getManifest.mockResolvedValue({
        schema_version: "1",
        status: "ready",
        version_id: "version-1",
        selected_preview_attempt_id: null,
        variants: [
          {
            kind: "audio",
            media_type: "audio/wav",
            delivery: "inline",
            renderer_contract: "core.audio.v1",
            rank: 200,
            content_url:
              "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/source",
            codecs: [],
          },
          {
            kind: "markdown",
            media_type: "text/markdown",
            delivery: "text",
            renderer_contract: "core.markdown.v1",
            rank: 100,
            text: "Converted transcript",
          },
        ],
        fallbacks: [],
      });

      render(
        <PreviewPane
          detail={libraryDetail()}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );

      const audio = await screen.findByLabelText("Audio preview for Manifest item");
      expect(audio).toBeVisible();
      expect(screen.queryByText("Converted transcript")).toBeNull();

      const originalButton = screen.getByRole("button", { name: "Original" });
      const convertedButton = screen.getByRole("button", { name: "Converted" });
      expect(originalButton).toHaveAttribute("aria-pressed", "true");
      expect(convertedButton).toHaveAttribute("aria-pressed", "false");

      fireEvent.click(convertedButton);

      expect(await screen.findByText("Converted transcript")).toBeInTheDocument();
      // Doc §3-F: the Original tree is hidden, not torn down and rebuilt --
      // a real PDF/media renderer would otherwise lose page position or
      // playback state on every toggle. Same DOM node, just `hidden`.
      expect(screen.getByLabelText("Audio preview for Manifest item")).toBe(audio);
      expect(audio).not.toBeVisible();
      expect(convertedButton).toHaveAttribute("aria-pressed", "true");

      fireEvent.click(originalButton);

      expect(screen.getByLabelText("Audio preview for Manifest item")).toBe(audio);
      expect(audio).toBeVisible();
    });

    it("offers a cross-link to the converted output once the only original renderer fails", async () => {
      previewApi.getManifest.mockResolvedValue({
        schema_version: "1",
        status: "ready",
        version_id: "version-1",
        selected_preview_attempt_id: null,
        variants: [
          {
            kind: "audio",
            media_type: "audio/wav",
            delivery: "inline",
            renderer_contract: "core.audio.v1",
            rank: 200,
            content_url:
              "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/source",
            codecs: [],
          },
          {
            kind: "text",
            media_type: "text/plain",
            delivery: "inline",
            renderer_contract: "core.text.v1",
            rank: 100,
            text: "Converted transcript",
          },
        ],
        fallbacks: [],
      });

      render(
        <PreviewPane
          detail={libraryDetail()}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );

      const audio = await screen.findByLabelText("Audio preview for Manifest item");
      fireEvent.error(audio);

      const crossLink = await screen.findByRole("button", {
        name: "View converted →",
      });
      fireEvent.click(crossLink);

      expect(await screen.findByText("Converted transcript")).toBeInTheDocument();
    });
  });

  // §3-D: Library always shows the item's own latest version. There is no
  // more History-driven selection to bind to (the old P10.2 contract), so
  // the only thing left to prove here is that a second, older version on
  // the same item never leaks into what's fetched or rendered.
  it("always fetches and renders the current version, never an older one on the same item", async () => {
    previewApi.getManifest.mockImplementation((_workspaceId, versionId) =>
      Promise.resolve({
        schema_version: "1" as const,
        status: "ready" as const,
        version_id: versionId,
        selected_preview_attempt_id: null,
        variants: [
          {
            kind: "text",
            media_type: "text/plain",
            delivery: "inline",
            renderer_contract: "core.text.v1",
            rank: 10,
            text: versionId === "version-1" ? "Older version content" : "Current version content",
          },
        ],
        fallbacks: [],
      }),
    );

    render(
      <PreviewPane
        detail={twoVersionLibraryDetail()}
        legacyPreviewKind={null}
        workspaceId="workspace-1"
      />,
    );

    expect(await screen.findByText("Current version content")).toBeInTheDocument();
    expect(screen.queryByText("Older version content")).toBeNull();
    expect(previewApi.getManifest).toHaveBeenCalledWith(
      "workspace-1",
      "version-2",
      expect.any(AbortSignal),
    );
  });

  describe("rendition preference across versions (doc §3-C/§3-D)", () => {
    it("does not carry an explicit pick across a new latest version replacing the current one", async () => {
      previewApi.getManifest.mockImplementation((_workspaceId, versionId) =>
        Promise.resolve(
          versionId === "version-3"
            ? {
                schema_version: "1" as const,
                status: "ready" as const,
                version_id: versionId,
                selected_preview_attempt_id: null,
                variants: [
                  {
                    kind: "audio",
                    media_type: "audio/wav",
                    delivery: "inline",
                    renderer_contract: "core.audio.v1",
                    rank: 200,
                    content_url: "/api/v1/workspaces/workspace-1/library/versions/version-3/preview/source",
                    codecs: [],
                  },
                ],
                fallbacks: [],
              }
            : {
                schema_version: "1" as const,
                status: "ready" as const,
                version_id: versionId,
                selected_preview_attempt_id: null,
                variants: [
                  {
                    kind: "audio",
                    media_type: "audio/wav",
                    delivery: "inline",
                    renderer_contract: "core.audio.v1",
                    rank: 200,
                    content_url: "/api/v1/workspaces/workspace-1/library/versions/version-2/preview/source",
                    codecs: [],
                  },
                  {
                    kind: "markdown",
                    media_type: "text/markdown",
                    delivery: "text",
                    renderer_contract: "core.markdown.v1",
                    rank: 100,
                    text: "Current version transcript",
                  },
                ],
                fallbacks: [],
              },
        ),
      );
      const detail = twoVersionLibraryDetail();

      const { rerender } = render(
        <PreviewPane
          detail={detail}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );
      await screen.findByLabelText("Audio preview for Manifest item");
      fireEvent.click(await screen.findByRole("button", { name: "Converted" }));
      expect(await screen.findByText("Current version transcript")).toBeInTheDocument();

      // A new version-3 is uploaded and becomes current -- its own manifest
      // has no converted candidate at all, shown automatically (no click
      // needed, and no toggle, since only one side has anything for this
      // version). Doc §3-B/§3-F: the pick lives only within "this version's
      // screen", so the new latest version gets a fresh default rather than
      // inheriting the previous version's "Converted" choice.
      const newVersion: LibraryItemVersionPayload = {
        ...detail.current_version!,
        id: "version-3",
        version_no: 3,
        previous_version_id: "version-2",
        original_url:
          "/api/v1/workspaces/workspace-1/library/versions/version-3/content",
      };
      rerender(
        <PreviewPane
          detail={{
            ...detail,
            current_version_id: "version-3",
            current_version: newVersion,
            versions: [newVersion, ...detail.versions],
          }}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );

      expect(await screen.findByLabelText("Audio preview for Manifest item"))
        .toBeVisible();
      expect(screen.queryByText("Current version transcript")).toBeNull();
      expect(
        screen.queryByText(
          "No converted rendition exists for this version; showing the original.",
        ),
      ).toBeNull();
      expect(screen.queryByRole("group", { name: "Rendition" })).toBeNull();
    });

    it("keeps the pick across an unrelated re-render of the same version", async () => {
      previewApi.getManifest.mockResolvedValue({
        schema_version: "1",
        status: "ready",
        version_id: "version-1",
        selected_preview_attempt_id: null,
        variants: [
          {
            kind: "audio",
            media_type: "audio/wav",
            delivery: "inline",
            renderer_contract: "core.audio.v1",
            rank: 200,
            content_url:
              "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/source",
            codecs: [],
          },
          {
            kind: "markdown",
            media_type: "text/markdown",
            delivery: "text",
            renderer_contract: "core.markdown.v1",
            rank: 100,
            text: "Converted transcript",
          },
        ],
        fallbacks: [],
      });
      const detail = libraryDetail();

      const { rerender } = render(
        <PreviewPane
          detail={detail}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );
      await screen.findByLabelText("Audio preview for Manifest item");
      fireEvent.click(await screen.findByRole("button", { name: "Converted" }));
      expect(await screen.findByText("Converted transcript")).toBeInTheDocument();

      // Same item, same version -- e.g. a conversion-status poll landing a
      // new `detail` object with identical version data. That still re-runs
      // the manifest fetch (a transient "loading" flash, unrelated to this
      // feature), but the resolved `versionId` string is unchanged, so the
      // pick itself must survive once the fetch resettles.
      rerender(
        <PreviewPane
          detail={{ ...detail }}
          legacyPreviewKind={null}
          workspaceId="workspace-1"
        />,
      );

      expect(await screen.findByText("Converted transcript")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Converted" }),
      ).toHaveAttribute("aria-pressed", "true");
    });
  });

  describe("previewVersionId", () => {
    it("always resolves the item's own current version, ignoring every other version on it", () => {
      const detail = twoVersionLibraryDetail();
      expect(previewVersionId(detail)).toBe("version-2");
    });

    it("falls back to current_version_id when the embedded current_version object is absent", () => {
      const detail = twoVersionLibraryDetail();
      expect(
        previewVersionId({ ...detail, current_version: null }),
      ).toBe("version-2");
    });
  });
});

function twoVersionLibraryDetail(): LibraryItemDetail {
  const olderVersion = {
    id: "version-1",
    library_item_id: "item-1",
    version_no: 1,
    content_hash: "hash-1",
    processing_status: "ready",
    extraction_summary: null,
    previous_version_id: null,
    created_at: "2026-08-01T00:00:00Z",
    fetched_at: "2026-08-01T00:00:00Z",
    original_url:
      "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
  };
  const currentVersion = {
    id: "version-2",
    library_item_id: "item-1",
    version_no: 2,
    content_hash: "hash-2",
    processing_status: "ready",
    extraction_summary: null,
    previous_version_id: "version-1",
    created_at: "2026-08-09T00:00:00Z",
    fetched_at: "2026-08-09T00:00:00Z",
    original_url:
      "/api/v1/workspaces/workspace-1/library/versions/version-2/content",
  };
  return {
    id: "item-1",
    uri: "library://item-1",
    title: "Manifest item",
    connector_type: "upload",
    format_hint: "txt",
    trust_level: "trusted",
    lifecycle_status: "active",
    current_version_id: "version-2",
    updated_at: "2026-08-09T00:00:00Z",
    metadata: {},
    current_version: currentVersion,
    versions: [currentVersion, olderVersion],
    artifacts: [],
  };
}

function mediaManifest() {
  return {
    schema_version: "1",
    status: "ready",
    version_id: "version-1",
    selected_preview_attempt_id: null,
    variants: [
      {
        kind: "audio",
        media_type: "audio/wav",
        delivery: "inline",
        renderer_contract: "core.audio.v1",
        rank: 10,
        content_url:
          "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/source",
        codecs: [],
      },
    ],
    fallbacks: ["download"],
    download_url:
      "/api/v1/workspaces/workspace-1/library/versions/version-1/content",
  };
}

function libraryDetail(processingStatus = "ready"): LibraryItemDetail {
  const version = {
    id: "version-1",
    library_item_id: "item-1",
    version_no: 1,
    content_hash: "hash-1",
    processing_status: processingStatus,
    extraction_summary: null,
    previous_version_id: null,
    created_at: "2026-08-09T00:00:00Z",
    fetched_at: "2026-08-09T00:00:00Z",
  };
  return {
    id: "item-1",
    uri: "library://item-1",
    title: "Manifest item",
    connector_type: "upload",
    format_hint: "txt",
    trust_level: "trusted",
    lifecycle_status: "active",
    current_version_id: "version-1",
    current_preview_version_id: "version-1",
    updated_at: "2026-08-09T00:00:00Z",
    metadata: {},
    current_version: version,
    versions: [version],
    artifacts: [],
  };
}
