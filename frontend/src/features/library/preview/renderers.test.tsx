import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PreviewTransport } from "./AuthenticatedPreviewTransport";

vi.mock("../PdfLibraryPreview", () => ({
  PdfLibraryPreview: ({ onLoadError }: { onLoadError?: (error: Error) => void }) => (
    <button onClick={() => onLoadError?.(new Error("broken PDF"))} type="button">
      Fail PDF
    </button>
  ),
}));

const transport: PreviewTransport = {
  contentUrl: vi.fn((value: string) => value),
  fetch: vi.fn(async () => new Response("")),
  pdfSource: vi.fn((value: string) => ({
    url: value,
    withCredentials: true as const,
  })),
  mediaSource: vi.fn((value: string) => value),
};

describe("preview renderer module", () => {
  it("loads the trusted renderer bundle", async () => {
    const renderers = await import("./renderers");
    expect(renderers.MarkdownPreviewRenderer).toBeTypeOf("function");
  });

  it("renders canonical Markdown table artifacts as an accessible GFM table", async () => {
    const { TablePreviewRenderer } = await import("./renderers");

    render(
      <TablePreviewRenderer
        onRendererError={vi.fn()}
        title="scores.csv"
        transport={transport}
        variant={{
          kind: "table",
          media_type: "text/markdown",
          delivery: "text",
          renderer_contract: "core.table.v1",
          rank: 10,
          text: [
            '<video controls src="https://beacon.example/table.mp4"></video>',
            "",
            "| Name | Score |",
            "| --- | ---: |",
            "| Ada | 10 |",
            "",
            "![Remote table image](https://beacon.example/table.png)",
          ].join("\n"),
        }}
      />,
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Ada" })).toBeInTheDocument();
    expect(document.querySelector("video")).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Remote table image" }))
      .not.toBeInTheDocument();
    expect(screen.getByText("Remote table image")).toBeInTheDocument();
  });

  it("isolates manifest Markdown artifacts from raw HTML and remote resources", async () => {
    const { MarkdownPreviewRenderer } = await import("./renderers");

    render(
      <MarkdownPreviewRenderer
        onRendererError={vi.fn()}
        title="unsafe.md"
        transport={transport}
        variant={{
          kind: "markdown",
          media_type: "text/markdown",
          delivery: "text",
          renderer_contract: "core.markdown.v1",
          rank: 10,
          text: [
            '<video controls src="https://beacon.example/preview.mp4"></video>',
            "",
            "![Remote beacon](https://beacon.example/preview.png)",
          ].join("\n"),
        }}
      />,
    );

    expect(document.querySelector("video")).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Remote beacon" }))
      .not.toBeInTheDocument();
    expect(screen.getByText("Remote beacon")).toBeInTheDocument();
  });

  it("applies the same isolation to compatibility Markdown artifacts", async () => {
    const { LegacyPreviewRenderer } = await import("./LegacyPreviewRenderer");

    render(
      <LegacyPreviewRenderer
        manifest={{
          schema_version: "1",
          status: "ready",
          version_id: "version-1",
          selected_preview_attempt_id: null,
          variants: [
            {
              kind: "markdown",
              media_type: "text/markdown",
              delivery: "inline",
              renderer_contract: "core.markdown.v1",
              rank: 10,
              text: [
                '<img alt="Raw beacon" src="https://beacon.example/raw.png">',
                "",
                "![Remote beacon](https://beacon.example/legacy.png)",
              ].join("\n"),
            },
          ],
          fallbacks: [],
        }}
        title="legacy.md"
        transport={transport}
      />,
    );

    expect(screen.queryByRole("img", { name: "Raw beacon" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Remote beacon" }))
      .not.toBeInTheDocument();
    expect(screen.getByText("Remote beacon")).toBeInTheDocument();
  });

  it("reports PDF load failures so the registry can try the next variant", async () => {
    const { PdfPreviewRenderer } = await import("./renderers");
    const onRendererError = vi.fn();

    render(
      <PdfPreviewRenderer
        onRendererError={onRendererError}
        title="broken.pdf"
        transport={transport}
        variant={{
          kind: "pdf",
          media_type: "application/pdf",
          delivery: "range",
          renderer_contract: "core.pdf.v1",
          rank: 10,
          content_url: "/api/v1/workspaces/w/library/i/versions/v/original",
        }}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Fail PDF" }));
    expect(onRendererError).toHaveBeenCalledWith(expect.any(Error));
  });

  it("streams cookie-authenticated images directly without whole-file blob preloading", async () => {
    const { ImagePreviewRenderer } = await import("./renderers");
    const fetchPreview = vi.mocked(transport.fetch);
    fetchPreview.mockClear();

    render(
      <ImagePreviewRenderer
        onRendererError={vi.fn()}
        title="diagram.png"
        transport={transport}
        variant={{
          kind: "image",
          media_type: "image/png",
          delivery: "range",
          renderer_contract: "core.image.v1",
          rank: 10,
          content_url: "/preview/source",
          width: 640,
          height: 480,
        }}
      />,
    );

    expect(screen.getByRole("img", { name: "diagram.png" }))
      .toHaveAttribute("src", "/preview/source");
    expect(fetchPreview).not.toHaveBeenCalled();
  });

  it("falls back instead of preloading token-only images", async () => {
    const { ImagePreviewRenderer } = await import("./renderers");
    const onRendererError = vi.fn();

    render(
      <ImagePreviewRenderer
        onRendererError={onRendererError}
        title="private.png"
        transport={{ ...transport, mediaSource: vi.fn(() => null) }}
        variant={{
          kind: "image",
          media_type: "image/png",
          delivery: "range",
          renderer_contract: "core.image.v1",
          rank: 10,
          content_url: "/preview/source",
        }}
      />,
    );

    await waitFor(() => expect(onRendererError).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "authentication" }),
    ));
  });

  it("attempts codec-advisory native audio with safe playback controls", async () => {
    const canPlayType = vi
      .spyOn(HTMLMediaElement.prototype, "canPlayType")
      .mockReturnValue("");
    const onRendererError = vi.fn();
    const { AudioPreviewRenderer } = await import("./renderers");

    render(
      <AudioPreviewRenderer
        onRendererError={onRendererError}
        title="voice.m4a"
        transport={transport}
        variant={{
          kind: "audio",
          media_type: "audio/mp4",
          delivery: "inline",
          renderer_contract: "core.audio.v1",
          rank: 10,
          content_url: "/preview/source",
          codecs: ["mp4a.40.2"],
        }}
      />,
    );

    const audio = screen.getByLabelText("Audio preview for voice.m4a");
    expect(canPlayType).toHaveBeenCalledWith(
      'audio/mp4; codecs="mp4a.40.2"',
    );
    expect(audio).toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "metadata");
    expect(audio).not.toHaveAttribute("autoplay");

    fireEvent.error(audio);
    expect(onRendererError).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "codec" }),
    );
    canPlayType.mockRestore();
  });

  it("classifies native media runtime failures without exposing transport details", async () => {
    const canPlayType = vi
      .spyOn(HTMLMediaElement.prototype, "canPlayType")
      .mockReturnValue("probably");
    const onRendererError = vi.fn();
    const { VideoPreviewRenderer } = await import("./renderers");

    render(
      <VideoPreviewRenderer
        onRendererError={onRendererError}
        title="demo.mp4"
        transport={transport}
        variant={{
          kind: "video",
          media_type: "video/mp4",
          delivery: "inline",
          renderer_contract: "core.video.v1",
          rank: 10,
          content_url: "/preview/source",
          codecs: ["avc1"],
        }}
      />,
    );

    const video = screen.getByLabelText("Video preview for demo.mp4");
    Object.defineProperty(video, "error", {
      configurable: true,
      value: { code: 2 },
    });
    fireEvent.error(video);

    expect(onRendererError).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "runtime" }),
    );
    expect(document.body).not.toHaveTextContent("preview_content_unavailable");
    canPlayType.mockRestore();
  });

  it("classifies token-only native media as an authentication fallback", async () => {
    const onRendererError = vi.fn();
    const { AudioPreviewRenderer } = await import("./renderers");
    const tokenOnlyTransport = {
      ...transport,
      mediaSource: vi.fn(() => null),
    };

    render(
      <AudioPreviewRenderer
        onRendererError={onRendererError}
        title="private.wav"
        transport={tokenOnlyTransport}
        variant={{
          kind: "audio",
          media_type: "audio/wav",
          delivery: "inline",
          renderer_contract: "core.audio.v1",
          rank: 10,
          content_url: "/preview/source",
          codecs: [],
        }}
      />,
    );

    await waitFor(() => expect(onRendererError).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "authentication" }),
    ));
    expect(document.body).not.toHaveTextContent("cookie authentication");
  });
});
