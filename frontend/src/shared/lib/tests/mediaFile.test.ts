import { describe, expect, it } from "vitest";

import { fileExtensionLabel, mediaFileCategory } from "../mediaFile";

describe("media file placeholder labels", () => {
  it("picks one of five kinds and shows the extension as text", () => {
    expect(
      [
        "image/webp",
        "audio/wav",
        "video/mp4",
        "application/pdf",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
      ].map(mediaFileCategory),
    ).toEqual(["image", "audio", "video", "document", "document", "document", "file"]);
    expect(fileExtensionLabel("release.notes.final.Docx", "application/zip")).toBe("DOCX");
    // No extension: a short subtype stands in, a vendor subtype does not.
    expect(fileExtensionLabel("scan", "image/png")).toBe("PNG");
    expect(
      fileExtensionLabel("export", "application/vnd.oasis.opendocument.text"),
    ).toBe("");
  });
});
