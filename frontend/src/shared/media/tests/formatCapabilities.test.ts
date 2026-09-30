import { describe, expect, it } from "vitest";

import {
  formatUploadByteLimit,
  uploadAcceptValue,
  uploadFileRejection,
  uploadPolicyForMediaTypes,
} from "../formatCapabilities";

describe("format capabilities", () => {
  const policy = uploadPolicyForMediaTypes(
    ["IMAGE/PNG", "image/jpeg", "image/gif", "IMAGE/WEBP", "image/png"],
    4,
  );

  it("derives one stable MIME and extension accept value", () => {
    expect(policy).toEqual({
      mediaTypes: ["image/png", "image/jpeg", "image/gif", "image/webp"],
      extensions: [".png", ".jpg", ".jpeg", ".gif", ".webp"],
      maxBytes: 4,
    });
    expect(uploadAcceptValue(policy)).toBe(
      ".png,.jpg,.jpeg,.gif,.webp,image/png,image/jpeg,image/gif,image/webp",
    );
  });

  it("accepts either an advertised MIME or extension within the size limit", () => {
    expect(
      uploadFileRejection(
        new File(["1234"], "clipboard-image", { type: "image/png" }),
        policy,
      ),
    ).toBeNull();
    expect(
      uploadFileRejection(
        new File(["1234"], "camera.JPG", { type: "application/octet-stream" }),
        policy,
      ),
    ).toBeNull();
    expect(
      uploadFileRejection(
        new File(["1234"], "animation.gif", { type: "image/gif" }),
        policy,
      ),
    ).toBeNull();
    expect(
      uploadFileRejection(
        new File(["1234"], "preview.WEBP", { type: "application/octet-stream" }),
        policy,
      ),
    ).toBeNull();
  });

  it("rejects unsupported and oversized files before upload", () => {
    expect(
      uploadFileRejection(
        new File(["1234"], "vector.svg", { type: "image/svg+xml" }),
        policy,
      ),
    ).toEqual({ reason: "unsupported_format" });
    expect(
      uploadFileRejection(
        new File(["12345"], "large.png", { type: "image/png" }),
        policy,
      ),
    ).toEqual({ reason: "too_large", maxBytes: 4 });
  });

  it("formats binary upload limits for user-facing validation", () => {
    expect(formatUploadByteLimit(10 * 1024 * 1024)).toBe("10 MiB");
    expect(formatUploadByteLimit(1536)).toBe("2 KiB");
  });
});
