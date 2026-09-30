import { EditorState, type Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImageUploadApiError } from "../../../shared/api/uploads";
import {
  cancelImageUpload,
  createEditorPasteHandlers,
  createUploadTargetSnapshot,
  htmlToMarkdown,
  insertImageFilesAtRange,
  markdownFromClipboard,
  removeImageUpload,
  retryImageUpload,
  type EditorPasteOptions,
  type ImageUploadContext,
  type UploadTargetSnapshot,
} from "../editorPaste";
import {
  editorUploadSlotsExtension,
  getEditorUploadSlots,
  hasEditorUploadSlot,
} from "../editorUploadSlots";

const CANONICAL_ASSET_ID = "00000000-0000-4000-8000-000000000999";
const CANONICAL_LOGICAL_REF = `dtx-media://asset/${CANONICAL_ASSET_ID}`;

function uploadEditorState(doc: string, extensions: Extension = []) {
  return EditorState.create({
    doc,
    extensions: [editorUploadSlotsExtension, extensions],
  });
}

function targetedUploadOptions(
  options: EditorPasteOptions = {},
  target: { pageId?: string; editingVersionId?: string } = {},
): EditorPasteOptions {
  const pageId = target.pageId ?? "page-1";
  const editingVersionId = target.editingVersionId ?? "version-1";
  return {
    createUploadTargetSnapshot(file, operationId) {
      return createUploadTargetSnapshot({
        actorSessionGeneration: 1,
        editingVersionId,
        editorGeneration: 1,
        file,
        operationId,
        pageId,
        policyRevision: "context-media-v1",
        workspaceId: "workspace-1",
      });
    },
    isUploadTargetCurrent: () => true,
    ...options,
  };
}

describe("editor paste conversion", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("converts common rich HTML into Markdown", () => {
    const markdown = htmlToMarkdown(`
      <h2>Decision</h2>
      <p><strong>Keep</strong> <em>Markdown</em> canonical.</p>
      <p><a href="https://example.com/docs">Docs</a></p>
      <pre><code class="language-ts">const ok = true;</code></pre>
    `);

    expect(markdown).toContain("## Decision");
    expect(markdown).toContain("**Keep** _Markdown_ canonical.");
    expect(markdown).toContain("[Docs](https://example.com/docs)");
    expect(markdown).toContain("```ts\nconst ok = true;\n```");
  });

  it("converts internal links and tables", () => {
    const markdown = htmlToMarkdown(`
      <p><a data-internal-slug="release-notes" href="/pages/release-notes">Release Notes</a></p>
      <table>
        <tr><th>Name</th><th>Status</th></tr>
        <tr><td>API</td><td>Ready</td></tr>
      </table>
    `);

    expect(markdown).toContain("[[release-notes|Release Notes]]");
    expect(markdown).toContain("| Name | Status |");
    expect(markdown).toContain("| --- | --- |");
    expect(markdown).toContain("| API | Ready |");
  });

  it("inserts the canonical context media logical reference after upload", async () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    const uploadImage = vi.fn().mockResolvedValue({
      asset_id: assetId,
      logical_ref: `dtx-media://asset/${assetId}`,
      content_url: `/api/v1/pages/page-1/versions/version-1/media/${assetId}/content`,
    });
    const view = new EditorView({
      state: uploadEditorState("Before\n"),
      parent: document.body,
    });

    try {
      expect(
        insertImageFilesAtRange(
          view,
          [new File(["image"], "diagram.png", { type: "image/png" })],
          targetedUploadOptions({ uploadImage }),
        ),
      ).toBe(true);
      expect(view.state.doc.toString()).toBe("Before\n");
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);

      await vi.waitFor(() => {
        expect(view.state.doc.toString()).toContain(
          `![diagram.png](dtx-media://asset/${assetId})`,
        );
      });
      expect(uploadImage).toHaveBeenCalledWith(
        expect.any(File),
        expect.objectContaining({
          marker: expect.any(String),
          editingVersionId: "version-1",
          pageId: "page-1",
          targetSnapshot: expect.objectContaining({
            assetOperationId: expect.any(String),
            editingVersionId: "version-1",
            pageId: "page-1",
            policyRevision: "context-media-v1",
            workspaceId: "workspace-1",
          }),
        }),
      );
      const context = uploadImage.mock.calls[0][1];
      expect(context.targetSnapshot.assetOperationId).toBe(context.marker);
      expect(Object.isFrozen(context.targetSnapshot)).toBe(true);
      expect(Object.isFrozen(context.targetSnapshot.payloadFingerprint)).toBe(true);
    } finally {
      view.destroy();
    }
  });

  it("rejects an image through the injected capability validator before upload", () => {
    const uploadImage = vi.fn();
    const onUploadError = vi.fn();
    const view = new EditorView({
      state: uploadEditorState("Before\n"),
      parent: document.body,
    });

    try {
      expect(
        insertImageFilesAtRange(
          view,
          [new File(["gif"], "animation.gif", { type: "image/gif" })],
          {
            onUploadError,
            uploadImage,
            validateImageFile: () => "Choose a PNG or JPEG image.",
          },
        ),
      ).toBe(true);
      expect(uploadImage).not.toHaveBeenCalled();
      expect(onUploadError).toHaveBeenCalledWith(
        expect.objectContaining({ name: "animation.gif" }),
        "Choose a PNG or JPEG image.",
      );
      expect(view.state.doc.toString()).toBe("Before\n");
    } finally {
      view.destroy();
    }
  });

  it("writes completed references in FileList order after inverse completion", async () => {
    const releases = new Map<string, (result: { logical_ref: string }) => void>();
    const uploadImage = vi.fn((file: File) =>
      new Promise<{ logical_ref: string }>((resolve) => {
        releases.set(file.name, resolve);
      }));
    const options = targetedUploadOptions({
      getUploadBatchCapabilities: () => ({
        policy_revision: "policy-v1",
        uploadable_media_types: ["image/png"],
        max_upload_bytes: 100,
        max_files_per_batch: 3,
        max_batch_bytes: 300,
        max_parallel_uploads: 3,
      }),
      uploadImage,
      validateImageFile: () => null,
    });
    const view = new EditorView({
      state: uploadEditorState("Before"),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(view, [
        new File(["a"], "a.png", { type: "image/png" }),
        new File(["b"], "b.png", { type: "image/png" }),
        new File(["c"], "c.png", { type: "image/png" }),
      ], options);
      expect(view.state.doc.toString()).toBe("Before");
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(3));

      releases.get("c.png")?.({ logical_ref: CANONICAL_LOGICAL_REF });
      await vi.waitFor(() => expect(view.state.doc.toString()).toContain("![c.png]"));
      releases.get("b.png")?.({ logical_ref: CANONICAL_LOGICAL_REF });
      await vi.waitFor(() => expect(view.state.doc.toString()).toContain("![b.png]"));
      releases.get("a.png")?.({ logical_ref: CANONICAL_LOGICAL_REF });
      await vi.waitFor(() => expect(view.state.doc.toString()).toContain("![a.png]"));

      const body = view.state.doc.toString();
      expect(body.indexOf("![a.png]")).toBeLessThan(body.indexOf("![b.png]"));
      expect(body.indexOf("![b.png]")).toBeLessThan(body.indexOf("![c.png]"));
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
    } finally {
      view.destroy();
    }
  });

  it("retries a failed upload at its original marker", async () => {
    const uploadImage = vi.fn()
      .mockRejectedValueOnce(new Error("raw req-secret"))
      .mockResolvedValueOnce({ logical_ref: CANONICAL_LOGICAL_REF });
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({ onUploadStateChange, uploadImage });
    const view = new EditorView({
      state: uploadEditorState("Before\n"),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["image"], "retry.png", { type: "image/png" })],
        options,
      );
      await vi.waitFor(() => {
        expect(onUploadStateChange).toHaveBeenCalledWith(
          expect.objectContaining({ name: "retry.png" }),
          "failed",
          "Request failed. Please try again.",
        );
      });
      expect(
        onUploadStateChange.mock.calls.flatMap((call) => call).join(" "),
      ).not.toContain("raw req-secret");
      expect(view.state.doc.toString()).toBe("Before\n");
      expect(getEditorUploadSlots(view.state)).toHaveLength(1);
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      expect(view.state.doc.toString()).not.toContain("upload failed");
      expect(view.state.doc.toString()).not.toContain("upload cancelled");
      const failedCall = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "failed",
      );
      expect(failedCall).toBeDefined();

      expect(retryImageUpload(view, failedCall?.[0], options)).toBe(true);
      await vi.waitFor(() => {
        expect(view.state.doc.toString()).toContain(
          `![retry.png](${CANONICAL_LOGICAL_REF})`,
        );
      });
      expect(onUploadStateChange.mock.calls.map(([, status]) => status)).toEqual([
        "queued",
        "uploading",
        "failed",
        "queued",
        "uploading",
        "succeeded",
      ]);
      expect(uploadImage).toHaveBeenCalledTimes(2);
      expect(uploadImage.mock.calls[0][1].marker).toBe(
        uploadImage.mock.calls[1][1].marker,
      );
      expect(uploadImage.mock.calls[0][1].targetSnapshot).toBe(
        uploadImage.mock.calls[1][1].targetSnapshot,
      );
    } finally {
      view.destroy();
    }
  });

  it("keeps active-cancel outcome-unknown uploads transient until explicit Remove", async () => {
    const uploadImage = vi.fn(
      (_file: File, context: { signal: AbortSignal }) =>
        new Promise<never>((_resolve, reject) => {
          context.signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({ onUploadStateChange, uploadImage });
    const view = new EditorView({
      state: uploadEditorState("Before\n"),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["image"], "cancel.png", { type: "image/png" })],
        options,
      );
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      const queuedCall = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "queued",
      );
      const upload = queuedCall?.[0];
      expect(upload).toBeDefined();

      expect(cancelImageUpload(options, upload.marker)).toBe(true);
      await vi.waitFor(() => {
        expect(onUploadStateChange).toHaveBeenCalledWith(
          upload,
          "outcome-unknown",
          undefined,
        );
      });
      expect(view.state.doc.toString()).toBe("Before\n");
      expect(hasEditorUploadSlot(view, upload.marker)).toBe(true);
      expect(view.state.doc.toString()).not.toContain("upload cancelled");
      expect(view.state.doc.toString()).not.toContain("<!--");

      expect(removeImageUpload(view, upload, options)).toBe(true);
      expect(hasEditorUploadSlot(view, upload.marker)).toBe(false);
      expect(view.state.doc.toString()).toBe("Before\n");
    } finally {
      view.destroy();
    }
  });

  it("preflights a mixed batch against the captured capability limits", () => {
    const uploadImage = vi.fn();
    const onUploadError = vi.fn();
    const view = new EditorView({
      state: uploadEditorState(""),
      parent: document.body,
    });
    try {
      const options = targetedUploadOptions({
        getUploadBatchCapabilities: () => ({
          policy_revision: "policy-v1",
          uploadable_media_types: ["image/png"],
          max_upload_bytes: 10,
          max_files_per_batch: 2,
          max_batch_bytes: 20,
          max_parallel_uploads: 2,
        }),
        onUploadError,
        uploadImage,
        validateImageFile: (file) => file.type === "image/png" ? null : "PNG only.",
      });
      expect(insertImageFilesAtRange(view, [
        new File(["ok"], "ok.png", { type: "image/png" }),
        new File(["bad"], "bad.gif", { type: "image/gif" }),
      ], options)).toBe(true);
      expect(view.state.doc.toString()).toBe("");
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|bad\.gif|<!--/);
      expect(getEditorUploadSlots(view.state)).toHaveLength(1);
      expect(onUploadError).toHaveBeenCalledWith(
        expect.objectContaining({ name: "bad.gif" }),
        "PNG only.",
        "validation_failed",
      );
    } finally {
      view.destroy();
    }
  });

  it("atomically re-preflights the original batch and replays only stale-policy failures", async () => {
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000811")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000812");
    let capabilities = {
      policy_revision: "policy-v1",
      uploadable_media_types: ["image/png"],
      max_upload_bytes: 100,
      max_files_per_batch: 2,
      max_batch_bytes: 200,
      max_parallel_uploads: 2,
    };
    const refreshUploadPolicy = vi.fn(async () => {
      capabilities = { ...capabilities, policy_revision: "policy-v2" };
      return capabilities;
    });
    const uploadImage = vi.fn(async (_file: File, context: ImageUploadContext) => {
      if (context.targetSnapshot.policyRevision === "policy-v1") {
        throw new ImageUploadApiError(409, {
          error_code: "media_policy_changed",
          message: "Refresh upload policy.",
          request_id: "request-policy",
          warnings: [],
        }, null);
      }
      return { logical_ref: CANONICAL_LOGICAL_REF };
    });
    const options = targetedUploadOptions({
      createUploadTargetSnapshot(file, operationId) {
        return createUploadTargetSnapshot({
          actorSessionGeneration: 1,
          editingVersionId: "version-1",
          editorGeneration: 1,
          file,
          operationId,
          pageId: "page-1",
          policyRevision: capabilities.policy_revision,
          workspaceId: "workspace-1",
        });
      },
      getUploadBatchCapabilities: () => capabilities,
      isUploadCapabilityCurrent: () => true,
      refreshUploadPolicy,
      uploadImage,
      validateImageFile: () => null,
    });
    const view = new EditorView({
      state: uploadEditorState(""),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(view, [
        new File(["a"], "a.png", { type: "image/png" }),
        new File(["b"], "b.png", { type: "image/png" }),
      ], options);

      await vi.waitFor(() => {
        expect(view.state.doc.toString()).toContain(
          `![a.png](${CANONICAL_LOGICAL_REF})`,
        );
        expect(view.state.doc.toString()).toContain(
          `![b.png](${CANONICAL_LOGICAL_REF})`,
        );
      });
      expect(refreshUploadPolicy).toHaveBeenCalledTimes(1);
      expect(uploadImage).toHaveBeenCalledTimes(4);
      for (const fileName of ["a.png", "b.png"]) {
        const calls = uploadImage.mock.calls.filter(([file]) => file.name === fileName);
        expect(calls).toHaveLength(2);
        expect(calls[0][1].marker).toBe(calls[1][1].marker);
        expect(calls[0][1].targetSnapshot).not.toBe(calls[1][1].targetSnapshot);
        expect(calls.map(([, context]) => context.targetSnapshot.policyRevision))
          .toEqual(["policy-v1", "policy-v2"]);
      }
    } finally {
      view.destroy();
    }
  });

  it("blocks every automatic replay when fresh policy rejects the original batch", async () => {
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000821")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000822");
    let capabilities = {
      policy_revision: "policy-v1",
      uploadable_media_types: ["image/png"],
      max_upload_bytes: 100,
      max_files_per_batch: 2,
      max_batch_bytes: 200,
      max_parallel_uploads: 2,
    };
    const onUploadBatchesChange = vi.fn();
    const uploadImage = vi.fn(async () => {
      throw new ImageUploadApiError(409, {
        error_code: "media_policy_changed",
        message: "Refresh upload policy.",
        request_id: "request-policy",
        warnings: [],
      }, null);
    });
    const options = targetedUploadOptions({
      createUploadTargetSnapshot(file, operationId) {
        return createUploadTargetSnapshot({
          actorSessionGeneration: 1,
          editingVersionId: "version-1",
          editorGeneration: 1,
          file,
          operationId,
          pageId: "page-1",
          policyRevision: capabilities.policy_revision,
          workspaceId: "workspace-1",
        });
      },
      getUploadBatchCapabilities: () => capabilities,
      isUploadCapabilityCurrent: () => true,
      onUploadBatchesChange,
      refreshUploadPolicy: async () => {
        capabilities = {
          ...capabilities,
          max_files_per_batch: 1,
          policy_revision: "policy-v2",
        };
        return capabilities;
      },
      uploadImage,
      validateImageFile: () => null,
    });
    const view = new EditorView({
      state: uploadEditorState(""),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(view, [
        new File(["a"], "a.png", { type: "image/png" }),
        new File(["b"], "b.png", { type: "image/png" }),
      ], options);

      await vi.waitFor(() => {
        const latest = onUploadBatchesChange.mock.calls.at(-1)?.[0]?.[0];
        expect(latest?.policyRejectionMessage).toContain("at most 1 file");
      });
      expect(uploadImage).toHaveBeenCalledTimes(2);
      expect(view.state.doc.toString()).toBe("");
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      expect(getEditorUploadSlots(view.state)).toHaveLength(2);
    } finally {
      view.destroy();
    }
  });

  it("freezes enqueue-time targets before sequential tasks run", async () => {
    let capturedWorkspace = "workspace-original";
    let capturedPage = "page-original";
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const uploadImage = vi.fn(async (_file: File, _context: ImageUploadContext) => {
      if (uploadImage.mock.calls.length === 1) {
        await firstGate;
      }
      return { logical_ref: CANONICAL_LOGICAL_REF };
    });
    const options = targetedUploadOptions({
      createUploadTargetSnapshot(file, operationId) {
        return createUploadTargetSnapshot({
          actorSessionGeneration: 7,
          editingVersionId: "version-original",
          editorGeneration: 11,
          file,
          operationId,
          pageId: capturedPage,
          policyRevision: "policy-original",
          workspaceId: capturedWorkspace,
        });
      },
      uploadImage,
    });
    const view = new EditorView({
      state: uploadEditorState(""),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [
          new File(["first"], "first.png", { type: "image/png" }),
          new File(["second"], "second.png", { type: "image/png" }),
        ],
        options,
      );
      capturedWorkspace = "workspace-stale";
      capturedPage = "page-stale";
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      releaseFirst();
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(2));

      for (const [, context] of uploadImage.mock.calls) {
        expect(context.targetSnapshot).toMatchObject({
          editingVersionId: "version-original",
          pageId: "page-original",
          policyRevision: "policy-original",
          workspaceId: "workspace-original",
        });
      }
    } finally {
      view.destroy();
    }
  });

  it("rejects incomplete policy and generation metadata before enqueue", () => {
    const file = new File(["image"], "snapshot.png", { type: "image/png" });
    const snapshotInput = {
      actorSessionGeneration: 1,
      editingVersionId: "version-1",
      editorGeneration: 1,
      file,
      operationId: "operation-1",
      pageId: "page-1",
      policyRevision: "policy-1",
      workspaceId: "workspace-1",
    };

    expect(() => createUploadTargetSnapshot({
      ...snapshotInput,
      policyRevision: "",
    })).toThrow("Upload target policy revision is not available.");
    expect(() => createUploadTargetSnapshot({
      ...snapshotInput,
      editorGeneration: Number.NaN,
    })).toThrow("Upload target generation is invalid.");
    expect(() => createUploadTargetSnapshot({
      ...snapshotInput,
      pageId: "",
    })).toThrow("Upload target page is not available.");
    expect(() => createUploadTargetSnapshot({
      ...snapshotInput,
      editingVersionId: "",
    })).toThrow("Upload target editing version is not available.");
  });

  it("ignores a late completion after its editor generation becomes stale", async () => {
    let current = true;
    let resolveUpload!: (result: { logical_ref: string }) => void;
    const uploadImage = vi.fn(
      (_file: File, _context: ImageUploadContext) =>
        new Promise<{ logical_ref: string }>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({
      isUploadTargetCurrent: () => current,
      onUploadStateChange,
      uploadImage,
    });
    const view = new EditorView({
      state: uploadEditorState("Before"),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["late"], "late.png", { type: "image/png" })],
        options,
      );
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      const bodyBeforeCompletion = view.state.doc.toString();
      current = false;
      resolveUpload({ logical_ref: CANONICAL_LOGICAL_REF });
      await Promise.resolve();
      await Promise.resolve();

      expect(view.state.doc.toString()).toBe(bodyBeforeCompletion);
      expect(view.state.doc.toString()).not.toContain("stale.png)");
      expect(onUploadStateChange.mock.calls.map(([, status]) => status))
        .not.toContain("succeeded");
    } finally {
      view.destroy();
    }
  });

  it("does not mutate Markdown when a removed slot completes late", async () => {
    let resolveUpload!: (result: { logical_ref: string }) => void;
    const uploadImage = vi.fn(() =>
      new Promise<{ logical_ref: string }>((resolve) => {
        resolveUpload = resolve;
      }));
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({ onUploadStateChange, uploadImage });
    const view = new EditorView({
      state: uploadEditorState("Canonical"),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["late"], "late.png", { type: "image/png" })],
        options,
      );
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      const upload = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "uploading",
      )?.[0];
      expect(upload).toBeDefined();
      expect(removeImageUpload(view, upload, options)).toBe(true);
      expect(hasEditorUploadSlot(view, upload.marker)).toBe(false);

      resolveUpload({ logical_ref: CANONICAL_LOGICAL_REF });
      await vi.waitFor(() => {
        expect(onUploadStateChange.mock.calls.map(([, status]) => status))
          .toContain("outcome-unknown");
      });
      expect(view.state.doc.toString()).toBe("Canonical");
      expect(view.state.doc.toString()).not.toContain("late.png");
    } finally {
      view.destroy();
    }
  });

  it("invalidates an edited selected range and makes late completion Remove-only", async () => {
    let resolveFirst!: (result: { logical_ref: string }) => void;
    const uploadImage = vi.fn(() =>
      new Promise<{ logical_ref: string }>((resolve) => {
        resolveFirst = resolve;
      }));
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({ onUploadStateChange, uploadImage });
    const view = new EditorView({
      state: EditorState.create({
        doc: "before selected after",
        extensions: [createEditorPasteHandlers(options)],
      }),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["retry"], "retried.png", { type: "image/png" })],
        options,
        { from: 7, to: 15 },
      );
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      const upload = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "uploading",
      )?.[0];
      expect(upload).toBeDefined();
      expect(view.state.doc.toString()).toBe("before selected after");

      view.dispatch({ changes: { from: 8, insert: "edited-" } });
      expect(view.state.doc.toString()).toBe("before sedited-elected after");
      expect(hasEditorUploadSlot(view, upload.marker)).toBe(false);
      expect(upload.insertionSlotAvailable).toBe(false);
      resolveFirst({ logical_ref: CANONICAL_LOGICAL_REF });
      await vi.waitFor(() => {
        expect(onUploadStateChange.mock.calls.map(([, status]) => status))
          .toContain("outcome-unknown");
      });

      expect(retryImageUpload(view, upload, options)).toBe(false);
      expect(getEditorUploadSlots(view.state)).toHaveLength(0);
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      expect(view.state.doc.toString()).not.toContain("first-attempt.png");
    } finally {
      view.destroy();
    }
  });

  it("lets a preparation-stage invalidation be removed without creating a task", async () => {
    let resolveSnapshot!: (snapshot: UploadTargetSnapshot) => void;
    const snapshot = new Promise<UploadTargetSnapshot>((resolve) => {
      resolveSnapshot = resolve;
    });
    const uploadImage = vi.fn();
    const onUploadStateChange = vi.fn();
    const options = targetedUploadOptions({
      createUploadTargetSnapshot: () => snapshot,
      onUploadStateChange,
      uploadImage,
    });
    const view = new EditorView({
      state: EditorState.create({
        doc: "before selected after",
        extensions: [createEditorPasteHandlers(options)],
      }),
      parent: document.body,
    });

    try {
      const file = new File(["pending"], "pending.png", { type: "image/png" });
      insertImageFilesAtRange(view, [file], options, { from: 7, to: 15 });
      const upload = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "queued",
      )?.[0];
      expect(upload).toBeDefined();

      view.dispatch({ changes: { from: 9, insert: "edit" } });
      expect(upload.insertionSlotAvailable).toBe(false);
      expect(hasEditorUploadSlot(view, upload.marker)).toBe(false);
      expect(onUploadStateChange.mock.calls.map(([, status]) => status))
        .toContain("cancelled");
      expect(removeImageUpload(view, upload, options)).toBe(true);

      resolveSnapshot(createUploadTargetSnapshot({
        actorSessionGeneration: 1,
        editingVersionId: "version-1",
        editorGeneration: 1,
        file,
        operationId: upload.marker,
        pageId: "page-1",
        policyRevision: "context-media-v1",
        workspaceId: "workspace-1",
      }));
      await Promise.resolve();
      await Promise.resolve();
      expect(uploadImage).not.toHaveBeenCalled();
    } finally {
      view.destroy();
    }
  });

  it("does not policy-replay an upload after its selected range is invalidated", async () => {
    let capabilities = {
      policy_revision: "policy-v1",
      uploadable_media_types: ["image/png"],
      max_upload_bytes: 100,
      max_files_per_batch: 1,
      max_batch_bytes: 100,
      max_parallel_uploads: 1,
    };
    let resolveRefresh!: (value: typeof capabilities) => void;
    const refresh = new Promise<typeof capabilities>((resolve) => {
      resolveRefresh = resolve;
    });
    const onUploadStateChange = vi.fn();
    const uploadImage = vi.fn(async (_file: File, context: ImageUploadContext) => {
      if (context.targetSnapshot.policyRevision === "policy-v1") {
        throw new ImageUploadApiError(409, {
          error_code: "media_policy_changed",
          message: "Refresh upload policy.",
          request_id: "request-policy",
          warnings: [],
        }, null);
      }
      return { logical_ref: CANONICAL_LOGICAL_REF };
    });
    const options = targetedUploadOptions({
      createUploadTargetSnapshot(file, operationId) {
        return createUploadTargetSnapshot({
          actorSessionGeneration: 1,
          editingVersionId: "version-1",
          editorGeneration: 1,
          file,
          operationId,
          pageId: "page-1",
          policyRevision: capabilities.policy_revision,
          workspaceId: "workspace-1",
        });
      },
      getUploadBatchCapabilities: () => capabilities,
      isUploadCapabilityCurrent: () => true,
      onUploadStateChange,
      refreshUploadPolicy: () => refresh,
      uploadImage,
      validateImageFile: () => null,
    });
    const view = new EditorView({
      state: EditorState.create({
        doc: "before selected after",
        extensions: [createEditorPasteHandlers(options)],
      }),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["policy"], "policy.png", { type: "image/png" })],
        options,
        { from: 7, to: 15 },
      );
      await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledTimes(1));
      const upload = onUploadStateChange.mock.calls.find(
        ([, status]) => status === "failed",
      )?.[0];
      expect(upload).toBeDefined();

      view.dispatch({ changes: { from: 9, insert: "edit" } });
      expect(upload.insertionSlotAvailable).toBe(false);
      capabilities = { ...capabilities, policy_revision: "policy-v2" };
      resolveRefresh(capabilities);
      await Promise.resolve();
      await Promise.resolve();
      expect(uploadImage).toHaveBeenCalledTimes(1);
      expect(view.state.doc.toString()).not.toContain("replayed.png");
      expect(retryImageUpload(view, upload, options)).toBe(false);
    } finally {
      view.destroy();
    }
  });

  it.each([
    {
      label: "a closed code fence",
      source: "```text\nlong line\n```",
      caret: "```text\nlong".length,
      expectedPrefix: `\`\`\`text\nlong line\n\`\`\`\n\n![boundary.png](${CANONICAL_LOGICAL_REF})`,
    },
    {
      label: "a heading",
      source: "## Heading",
      caret: 3,
      expectedPrefix: `## Heading\n\n![boundary.png](${CANONICAL_LOGICAL_REF})`,
    },
    {
      label: "a list item",
      source: "- Keep this item",
      caret: 4,
      expectedPrefix: `- Keep this item\n\n![boundary.png](${CANONICAL_LOGICAL_REF})`,
    },
    {
      label: "inline text",
      source: "before after",
      caret: 6,
      expectedPrefix: `before\n\n![boundary.png](${CANONICAL_LOGICAL_REF})`,
    },
  ])("inserts a block attachment safely at $label in one completion transaction", async ({
    caret,
    expectedPrefix,
    source,
  }) => {
    let changedTransactions = 0;
    const options = targetedUploadOptions({
      uploadImage: vi.fn().mockResolvedValue({
        logical_ref: CANONICAL_LOGICAL_REF,
      }),
    });
    const view = new EditorView({
      state: uploadEditorState(source, [
        EditorView.updateListener.of((update) => {
          if (update.docChanged) changedTransactions += 1;
        }),
      ]),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["image"], "boundary.png", { type: "image/png" })],
        options,
        { from: caret, to: caret },
      );

      expect(view.state.doc.toString()).toBe(source);
      expect(view.state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);
      await vi.waitFor(() => {
        expect(view.state.doc.toString()).toContain(expectedPrefix);
      });
      expect(changedTransactions).toBe(1);
    } finally {
      view.destroy();
    }
  });

  it("rejects a legacy URL-only upload result without mutating Markdown", async () => {
    const onUploadStateChange = vi.fn();
    const uploadImage = vi.fn().mockResolvedValue({
      url: "/api/v1/uploads/workspace-1/legacy.png",
    });
    const view = new EditorView({
      state: uploadEditorState(""),
      parent: document.body,
    });

    try {
      insertImageFilesAtRange(
        view,
        [new File(["image"], "legacy.png", { type: "image/png" })],
        targetedUploadOptions({
          onUploadStateChange,
          uploadImage: uploadImage as unknown as NonNullable<
            EditorPasteOptions["uploadImage"]
          >,
        }),
      );
      await vi.waitFor(() => {
        expect(onUploadStateChange).toHaveBeenCalledWith(
          expect.objectContaining({ name: "legacy.png" }),
          "failed",
          "Request failed. Please try again.",
        );
      });
      expect(view.state.doc.toString()).toBe("");
      expect(view.state.doc.toString()).not.toContain("/api/v1/uploads");
    } finally {
      view.destroy();
    }
  });

  it("drops unsafe destinations and escapes token-looking text", () => {
    const markdown = htmlToMarkdown(`
      <p>[x](javascript:alert(1)) [[private|page]] @cite{source}</p>
      <p><a href="javascript:alert(1)">Unsafe link</a></p>
      <p><img src="data:image/svg+xml;base64,PHN2Zy8+" alt="Unsafe image"></p>
    `);

    expect(markdown).toContain("\\[x\\]\\(javascript:alert\\(1\\)\\)");
    // `@`, `<`, `>` are not CommonMark syntax — escaping them produced visible
    // backslashes on render. The wikilink/cite brackets and braces still need
    // escaping because they ARE significant to the dtxWiki token rules.
    expect(markdown).toContain("\\[\\[private\\|page\\]\\] @cite\\{source\\}");
    expect(markdown).toContain("Unsafe link");
    expect(markdown).toContain("Unsafe image");
    expect(markdown).not.toContain("](javascript:");
    expect(markdown).not.toContain("](data:");
  });

  it("does not escape email at-signs in pasted plain text", () => {
    // Bug fix: previously @ was over-escaped, leaving "alice\@example.com" in
    // the source which remark renders verbatim with the visible backslash.
    const markdown = htmlToMarkdown(
      "<p>Contact alice@example.com or bob+filter@example.org for help.</p>",
    );

    expect(markdown).toContain("Contact alice@example.com");
    expect(markdown).toContain("bob+filter@example.org");
    expect(markdown).not.toContain("\\@");
  });

  it("uses a longer code fence when pasted code contains backticks", () => {
    const markdown = htmlToMarkdown(`
      <pre><code>safe
\`\`\`
[x](javascript:alert(1))</code></pre>
    `);

    expect(markdown).toContain("````\nsafe\n```\n[x](javascript:alert(1))\n````");
  });

  it("bypasses HTML conversion when HTML has no structural tags and plain text matches", () => {
    // Slack / code editor often emits <span style="...">text</span> + identical text/plain.
    const result = markdownFromClipboard({
      files: [],
      getData: (type: string) =>
        type === "text/html"
          ? '<span style="color: red">user@example.com</span>'
          : type === "text/plain"
            ? "user@example.com"
            : "",
    });

    expect(result).toBeNull();
  });

  it("keeps HTML conversion when HTML carries structural markup (heading)", () => {
    const result = markdownFromClipboard({
      files: [],
      getData: (type: string) =>
        type === "text/html"
          ? "<h2>Heading</h2><p>Body</p>"
          : type === "text/plain"
            ? "Heading\nBody"
            : "",
    });

    expect(result).toContain("## Heading");
  });

  it("keeps HTML conversion when text/plain is absent (HTML-only paste)", () => {
    const result = markdownFromClipboard({
      files: [],
      getData: (type: string) =>
        type === "text/html" ? '<span style="color: red">styled</span>' : "",
    });

    // No text/plain to compare against — fall through to HTML rendering.
    expect(result).not.toBeNull();
  });
});
